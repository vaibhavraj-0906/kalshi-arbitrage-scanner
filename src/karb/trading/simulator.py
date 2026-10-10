"""A simulated exchange desk behind Kalshi's portfolio endpoints (ADR-0010).

Tests and the offline ``karb demo`` need an exchange that accepts real order requests without a
network or an account. ``SimulatedDesk`` answers the same signed endpoints the demo exchange
does -- orders (single and batched), fills, orders lookup, positions, balance, settlements -- and
matches immediate-or-cancel orders against the order books its host exchange serves publicly,
consuming the size it fills. It follows Kalshi's documented mechanics:

- ``bid`` buys YES against NO bids (YES ask = 1 - NO bid); ``ask`` buys NO against YES bids;
- each fill pays ``ceil_6dp(rate * C * P * (1 - P))`` and the balance is then floored to whole
  cents, the rounding fee that karb's worst-case model assumes;
- one signed position per market, so opposite contracts net at once for $1 a pair;
- a repeated ``client_order_id`` is refused (409, or a per-order error in a batch).

Signatures are verified against the public key of the credentials the client signs with, so
the whole authentication path runs in every test.
"""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from karb.core.clock import Clock
from karb.core.fixed import (
    CASH_DECIMALS,
    PRICE_DECIMALS,
    PRICE_SCALE,
    QTY_DECIMALS,
    Cash,
    Price,
    Qty,
    format_scaled,
)
from karb.trading.auth import signing_message

__all__ = ["Ladders", "SimulatedDesk"]

Ladders = Mapping[str, MutableMapping[str, list[list[str]]]]
"""ticker -> {"yes_dollars": [[price, size], ...], "no_dollars": [...]}, bids ascending."""

_CENT = 10_000  # $0.01 in Cash raw units


def _price_text(raw: int) -> str:
    return format_scaled(raw, PRICE_DECIMALS)


@dataclass
class _Fill:
    fill_id: str
    order_id: str
    ticker: str
    side: str  # outcome side: "yes" or "no"
    price: int  # raw $0.0001, of the side bought
    qty: int  # raw 0.01 contracts
    fee: int  # raw $0.000001


@dataclass
class SimulatedDesk:
    ladders: Ladders
    clock: Clock
    fee_rate: Callable[[str], Fraction]
    """Taker coefficient times multiplier for a ticker."""
    public_key: Ed25519PublicKey | RSAPublicKey | None = None
    key_id: str | None = None
    balance: int = 10_000_000_000
    """Raw cash, $10,000 by default."""
    event_of: Callable[[str], str] = lambda ticker: ticker.rsplit("-", 1)[0]
    results: Mapping[str, str] = field(default_factory=dict)
    """ticker -> "yes"/"no" once a market has settled, for GET /portfolio/settlements."""
    on_order: Callable[[str], None] | None = None
    """Called with the ticker before each order matches: lets a scenario move the market."""
    fee_scale: Fraction = Fraction(1)
    """Charge this multiple of the documented fee, to exercise karb's fee audit."""
    lose_responses: int = 0
    """Process this many order POSTs, then drop their responses as a reset connection would."""
    account_unreadable_after_orders: bool = False
    """Once any order exists, answer balance and positions reads with HTTP 503."""
    positions: dict[str, int] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    fills: list[_Fill] = field(default_factory=list)
    settled: list[dict[str, Any]] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)

    # ---- transport --------------------------------------------------------------------------

    def handle(self, request: httpx.Request, endpoint: str) -> httpx.Response:
        self.requests.append((request.method, endpoint))
        problem = self._authenticate(request)
        if problem:
            return _json(401, {"error": {"code": "authentication_error", "message": problem}})
        params = request.url.params
        if (
            self.account_unreadable_after_orders
            and self.orders
            and endpoint in ("/portfolio/balance", "/portfolio/positions")
        ):
            return _json(503, {"error": {"code": "service_unavailable", "message": "try later"}})
        if request.method == "POST" and endpoint == "/portfolio/events/orders/batched":
            body = json.loads(request.content)
            results = [self._place(order) for order in body.get("orders") or []]
            return self._respond(request, 201, {"orders": results})
        if request.method == "POST" and endpoint == "/portfolio/events/orders":
            result = self._place(json.loads(request.content))
            error = result.get("error")
            if error is not None:
                status = 409 if error["code"] == "duplicate_client_order_id" else 400
                return _json(status, {"error": error})
            return self._respond(request, 201, result)
        if request.method == "GET" and endpoint == "/portfolio/fills":
            wanted = params.get("order_id")
            fills = [self._fill_json(f) for f in self.fills if wanted in (None, f.order_id)]
            return _json(200, {"fills": fills, "cursor": ""})
        if request.method == "GET" and endpoint == "/portfolio/orders":
            ticker = params.get("ticker")
            orders = [o for o in self.orders if ticker in (None, o["ticker"])]
            return _json(200, {"orders": orders, "cursor": ""})
        if request.method == "GET" and endpoint == "/portfolio/positions":
            event = params.get("event_ticker")
            positions = [
                {"ticker": t, "position_fp": _signed_qty(q), "exchange_index": 0}
                for t, q in sorted(self.positions.items())
                if event in (None, self.event_of(t))
            ]
            return _json(200, {"market_positions": positions, "event_positions": [], "cursor": ""})
        if request.method == "GET" and endpoint == "/portfolio/balance":
            return _json(
                200,
                {
                    "balance": self.balance // _CENT,
                    "balance_dollars": format_scaled(self.balance, CASH_DECIMALS),
                    "portfolio_value": 0,
                    "updated_ts": int(self.clock.now().timestamp()),
                },
            )
        if request.method == "GET" and endpoint == "/portfolio/settlements":
            event = params.get("event_ticker")
            rows = [row for row in self.settled if event in (None, row["event_ticker"])]
            return _json(200, {"settlements": rows, "cursor": ""})
        return _json(404, {"error": {"code": "not_found", "message": endpoint}})

    def _respond(self, request: httpx.Request, status: int, payload: object) -> httpx.Response:
        if self.lose_responses > 0:
            self.lose_responses -= 1
            raise httpx.ReadError("connection reset after the order was accepted", request=request)
        return _json(status, payload)

    def _authenticate(self, request: httpx.Request) -> str:
        key = request.headers.get("KALSHI-ACCESS-KEY")
        timestamp = request.headers.get("KALSHI-ACCESS-TIMESTAMP")
        signature = request.headers.get("KALSHI-ACCESS-SIGNATURE")
        if not key or not timestamp or not signature:
            return "missing authentication headers"
        if self.key_id is not None and key != self.key_id:
            return "unknown key id"
        if self.public_key is None:
            return ""
        message = signing_message(
            int(timestamp), request.method, request.url.raw_path.decode("ascii")
        ).encode()
        raw = base64.b64decode(signature)
        try:
            if isinstance(self.public_key, Ed25519PublicKey):
                self.public_key.verify(raw, message)
            else:
                self.public_key.verify(
                    raw,
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
                    ),
                    hashes.SHA256(),
                )
        except InvalidSignature:
            return "bad signature"
        return ""

    # ---- matching ---------------------------------------------------------------------------

    def _place(self, request: Mapping[str, Any]) -> dict[str, Any]:
        client_id = str(request.get("client_order_id") or "")
        if client_id and any(o["client_order_id"] == client_id for o in self.orders):
            return {
                "client_order_id": client_id,
                "order_id": None,
                "error": {
                    "code": "duplicate_client_order_id",
                    "message": "an order with this client_order_id already exists",
                },
            }
        ticker = str(request["ticker"])
        if ticker not in self.ladders or request.get("time_in_force") != "immediate_or_cancel":
            return {
                "client_order_id": client_id,
                "order_id": None,
                "error": {"code": "invalid_order", "message": f"cannot trade {ticker} that way"},
            }
        if self.on_order is not None:
            self.on_order(ticker)
        buying_yes = request["side"] == "bid"
        limit = Price.parse(str(request["price"])).raw  # YES-leg price
        wanted = Qty.parse(str(request["count"])).raw
        order_id = f"sim-{len(self.orders) + 1:04d}"
        ladder = self.ladders[ticker]
        # Buying YES lifts NO bids (YES ask = 1 - NO bid); buying NO lifts YES bids.
        key = "no_dollars" if buying_yes else "yes_dollars"
        bids = [[Price.parse(p).raw, Qty.parse(q).raw] for p, q in ladder.get(key) or []]
        remaining = wanted
        made: list[_Fill] = []
        while remaining and bids:
            bid_price, size = bids[-1]
            price = PRICE_SCALE - bid_price  # what the bought side costs
            if (buying_yes and price > limit) or (not buying_yes and bid_price < limit):
                break
            take = min(remaining, size)
            made.append(self._fill(order_id, ticker, "yes" if buying_yes else "no", price, take))
            remaining -= take
            if take == size:
                bids.pop()
            else:
                bids[-1][1] = size - take
        ladder[key] = [[_price_text(p), format_scaled(q, QTY_DECIMALS)] for p, q in bids]
        filled = wanted - remaining
        record = {
            "order_id": order_id,
            "client_order_id": client_id,
            "ticker": ticker,
            "outcome_side": "yes" if buying_yes else "no",
            "book_side": "bid" if buying_yes else "ask",
            "type": "limit",
            "status": "executed" if remaining == 0 else "canceled",
            "yes_price_dollars": _price_text(limit),
            "no_price_dollars": _price_text(PRICE_SCALE - limit),
            "fill_count_fp": format_scaled(filled, QTY_DECIMALS),
            "remaining_count_fp": "0.00",
            "initial_count_fp": format_scaled(wanted, QTY_DECIMALS),
            "taker_fees_dollars": format_scaled(sum(f.fee for f in made), CASH_DECIMALS),
            "created_time": self.clock.now().isoformat(),
        }
        self.orders.append(record)
        result: dict[str, Any] = {
            "order_id": order_id,
            "client_order_id": client_id,
            "fill_count": format_scaled(filled, QTY_DECIMALS),
            "remaining_count": "0.00",
            "ts_ms": int(self.clock.now().timestamp() * 1000),
            "error": None,
        }
        if filled:
            notional = sum(f.price * f.qty for f in made)
            result["average_fill_price"] = f"{notional / filled / PRICE_SCALE:.4f}"
            result["average_fee_paid"] = f"{sum(f.fee for f in made) / filled * 100 / 1e6:.6f}"
        return result

    def _fill(self, order_id: str, ticker: str, side: str, price: int, qty: int) -> _Fill:
        rate = self.fee_rate(ticker) * self.fee_scale
        fee = math.ceil(rate * qty * price * (PRICE_SCALE - price) / PRICE_SCALE)
        notional = Price(price).notional(Qty(qty)).raw
        self.balance -= notional + fee
        self.balance -= self.balance % _CENT  # balances are whole cents: the rounding fee
        signed = qty if side == "yes" else -qty
        before = self.positions.get(ticker, 0)
        after = before + signed
        netted = (abs(before) + abs(signed) - abs(after)) // 2
        self.balance += Qty(netted).payout().raw
        if after:
            self.positions[ticker] = after
        else:
            self.positions.pop(ticker, None)
        fill = _Fill(f"fill-{len(self.fills) + 1:04d}", order_id, ticker, side, price, qty, fee)
        self.fills.append(fill)
        return fill

    def _fill_json(self, fill: _Fill) -> dict[str, Any]:
        yes_price = fill.price if fill.side == "yes" else PRICE_SCALE - fill.price
        return {
            "fill_id": fill.fill_id,
            "trade_id": fill.fill_id,
            "order_id": fill.order_id,
            "ticker": fill.ticker,
            "market_ticker": fill.ticker,
            "outcome_side": fill.side,
            "book_side": "bid" if fill.side == "yes" else "ask",
            "count_fp": format_scaled(fill.qty, QTY_DECIMALS),
            "yes_price_dollars": format_scaled(yes_price * 100, 6),
            "no_price_dollars": format_scaled((PRICE_SCALE - yes_price) * 100, 6),
            "fee_cost": format_scaled(fill.fee, CASH_DECIMALS),
            "is_taker": True,
        }

    def settle_all(self) -> None:
        """Pay out and close every position in a settled market, as the exchange does."""
        for ticker, position in sorted(self.positions.items()):
            if ticker not in self.results:
                continue
            won_yes = self.results[ticker] == "yes"
            pays = (position > 0) == won_yes
            revenue = abs(position) if pays else 0  # cents: one per 0.01 contract
            row = {
                "ticker": ticker,
                "event_ticker": self.event_of(ticker),
                "market_result": self.results[ticker],
                "yes_count_fp": _signed_qty(max(position, 0)),
                "no_count_fp": _signed_qty(max(-position, 0)),
                "revenue": revenue,
                "value": 100 if won_yes else 0,
                "fee_cost": "0.0000",
                "settled_time": self.clock.now().isoformat(),
            }
            self.settled.append(row)
            self.balance += revenue * _CENT
        self.positions = {t: q for t, q in self.positions.items() if t not in self.results}

    def total_cash(self) -> Cash:
        return Cash(self.balance)


def _signed_qty(raw: int) -> str:
    return format_scaled(raw, QTY_DECIMALS)


def _json(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode())
