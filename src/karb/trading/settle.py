"""Settling positions against what the exchange actually decided (ADR-0008, ADR-0010).

A market is final when its status is ``finalized``. ``determined`` -- a result announced but not
yet final -- can still be disputed or amended, so it waits. What one YES contract paid comes from
``settlement_value_dollars`` when present, which also covers voided or scalar settlements; NO is
paid the complement.

Settlement is also the scanner's only external audit. A position's guaranteed P&L was computed
from karb's reading of the contracts. If the exchange pays less than that guarantee, the reading
was wrong somewhere, and the trade is flagged as a model violation rather than averaged away.

For trades placed on an exchange, karb also asks the exchange what it paid
(``GET /portfolio/settlements``). The two must agree once the cash returned at execution by
netting YES against NO is counted: a disagreement is reported, never silently reconciled.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from karb.core.fixed import Cash, Price, Qty
from karb.exchange.client import KalshiClient, KalshiHTTPError
from karb.market.book import Side
from karb.store.codec import to_ns
from karb.store.database import RecordStore, SettlementRow, TradeOrderRow
from karb.trading.portfolio import fetch_settlements
from karb.wire.models import MarketWire

__all__ = [
    "FINAL_STATUS",
    "MarketResult",
    "SettleSummary",
    "fetch_market_results",
    "holdings",
    "market_result",
    "settle_open_trades",
    "settlement_payout",
]

FINAL_STATUS: Final = "finalized"
EXCHANGE_SESSIONS: Final = frozenset({"demo", "simulated"})
"""Session kinds whose trades the exchange itself can report on."""
_TICKERS_PER_REQUEST: Final = 50


@dataclass(frozen=True, slots=True)
class MarketResult:
    ticker: str
    status: str
    result: str
    yes_value: Price | None
    """What one YES contract pays, once known."""
    settled_at: datetime | None

    @property
    def final(self) -> bool:
        return self.status == FINAL_STATUS and self.yes_value is not None


def market_result(wire: MarketWire) -> MarketResult:
    value: Price | None = None
    if wire.settlement_value_dollars:
        value = Price.parse(wire.settlement_value_dollars)
    elif wire.result == "yes":
        value = Price.ONE
    elif wire.result == "no":
        value = Price.ZERO
    return MarketResult(wire.ticker, wire.status, wire.result, value, wire.settlement_ts)


async def fetch_market_results(
    client: KalshiClient, tickers: Sequence[str]
) -> dict[str, MarketResult]:
    """Current status and result of each market, falling back to the historical archive."""
    results: dict[str, MarketResult] = {}
    for start in range(0, len(tickers), _TICKERS_PER_REQUEST):
        batch = tickers[start : start + _TICKERS_PER_REQUEST]
        payload = await client.get("/markets", [("tickers", ",".join(batch))])
        for raw in payload.get("markets") or []:
            wire = MarketWire.model_validate(raw)
            results[wire.ticker] = market_result(wire)
    for ticker in tickers:
        if ticker in results:
            continue
        # Markets settled before Kalshi's historical cutoff leave the live endpoints.
        try:
            payload = await client.get(f"/historical/markets/{ticker}")
        except KalshiHTTPError as exc:
            if exc.status == 404:
                continue
            raise
        raw = payload.get("market")
        if isinstance(raw, dict):
            results[ticker] = market_result(MarketWire.model_validate(raw))
    return results


def holdings(orders: Iterable[TradeOrderRow]) -> list[tuple[str, Side, Qty]]:
    """Contracts held after entry and hedge, by market and side."""
    held: dict[tuple[str, Side], int] = defaultdict(int)
    for order in orders:
        if order.phase in ("entry", "hedge") and order.filled > 0:
            held[(order.ticker, Side(order.side))] += order.filled
    return [(ticker, side, Qty(qty)) for (ticker, side), qty in sorted(held.items())]


def settlement_payout(
    held: Iterable[tuple[str, Side, Qty]], results: Mapping[str, MarketResult]
) -> Cash | None:
    """What the position paid out, or ``None`` while any of its markets is not final."""
    total = Cash.ZERO
    for ticker, side, qty in held:
        result = results.get(ticker)
        if result is None or not result.final or result.yes_value is None:
            return None
        value = result.yes_value if side is Side.YES else result.yes_value.complement()
        total = total + value.notional(qty)
    return total


@dataclass
class SettleSummary:
    checked: int = 0
    settled: int = 0
    pending: int = 0
    violations: list[str] = field(default_factory=list)
    realized: Cash = Cash.ZERO
    markets: int = 0
    exchange_checked: int = 0
    exchange_mismatches: list[str] = field(default_factory=list)


async def exchange_revenue(client: KalshiClient, event_ticker: str, tickers: Iterable[str]) -> Cash:
    """What the exchange paid at settlement for ``tickers``, by its own account."""
    wanted = set(tickers)
    cents = sum(
        int(row.get("revenue") or 0)
        for row in await fetch_settlements(client, event_ticker)
        if row.get("ticker") in wanted
    )
    return Cash(cents * 10_000)


async def settle_open_trades(
    store: RecordStore, client: KalshiClient, *, session_id: str | None = None
) -> SettleSummary:
    """Settle every open trade whose markets are all final."""
    summary = SettleSummary()
    trades = store.trades(session_id=session_id, status="open")
    kinds = {session.session_id: session.kind for session in store.sessions()}
    positions = {trade.trade_id: holdings(store.trade_orders(trade.trade_id)) for trade in trades}
    tickers = sorted({ticker for held in positions.values() for ticker, _, _ in held})
    summary.markets = len(tickers)
    results = await fetch_market_results(client, tickers) if tickers else {}
    now_ns = to_ns(client.clock.now())
    store.upsert_settlements(
        [
            SettlementRow(
                result.ticker,
                result.status,
                result.result,
                None if not result.final or result.yes_value is None else result.yes_value.raw,
                None if result.settled_at is None else to_ns(result.settled_at),
                now_ns,
            )
            for result in results.values()
        ]
    )
    for trade in trades:
        summary.checked += 1
        held = positions[trade.trade_id]
        payout = settlement_payout(held, results)
        if payout is None:
            summary.pending += 1
            continue
        realized = payout.raw - trade.total_cost
        violation = realized < trade.worst_after_hedge
        settled_at = [
            moment for ticker, _, _ in held if (moment := results[ticker].settled_at) is not None
        ]
        store.settle_trade(
            trade.trade_id,
            settled_ns=max((to_ns(moment) for moment in settled_at), default=now_ns),
            payout=payout.raw,
            realized_pnl=realized,
            model_violation=violation,
        )
        summary.settled += 1
        summary.realized = summary.realized + Cash(realized)
        if violation:
            summary.violations.append(trade.trade_id)
        if client.authenticated and kinds.get(trade.session_id) in EXCHANGE_SESSIONS:
            paid = await exchange_revenue(
                client, trade.event_ticker.split("#", 1)[0], {t for t, _, _ in held}
            )
            expected = payout - Cash(trade.netted_cash or 0)
            summary.exchange_checked += 1
            if paid != expected:
                summary.exchange_mismatches.append(
                    f"{trade.trade_id}: the exchange paid {paid.dollars()}, the model "
                    f"{expected.dollars()} after {Cash(trade.netted_cash or 0).dollars()} netted"
                )
    return summary
