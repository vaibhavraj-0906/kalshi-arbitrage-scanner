"""The signed portfolio endpoints: orders, fills, balance, positions, settlements (ADR-0010).

Placing an order is the one request whose outcome can be unknown: the connection may drop after
the exchange accepted it. Every order therefore carries a deterministic ``client_order_id``, and
any order without a clean result -- an error, a lost response, a retry answered with 409 -- is
looked up by that id before karb decides what it holds. An order is only recorded as unplaced
when the exchange has no record of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from karb.core.fixed import QTY_DECIMALS, Cash, Qty, parse_scaled
from karb.exchange.client import KalshiClient, KalshiError
from karb.market.fees import CENT_BALANCE_UNIT
from karb.trading.orders import (
    ExchangeFill,
    Order,
    PlacedOrder,
    client_order_id,
    order_request,
    parse_fill,
)

__all__ = [
    "BATCH_PATH",
    "ORDERS_PATH",
    "fetch_balance",
    "fetch_fills",
    "fetch_positions",
    "fetch_settlements",
    "find_order",
    "place_orders",
]

ORDERS_PATH: Final = "/portfolio/events/orders"
BATCH_PATH: Final = "/portfolio/events/orders/batched"
_FILL_ATTEMPTS: Final = 4


async def fetch_fills(client: KalshiClient, order_id: str) -> tuple[ExchangeFill, ...]:
    fills: list[ExchangeFill] = []
    async for page in client.paginate(
        "/portfolio/fills", [("order_id", order_id), ("limit", 1000)], "fills", auth=True
    ):
        fills.extend(parse_fill(raw) for raw in page)
    return tuple(fills)


async def find_order(
    client: KalshiClient, ticker: str, client_id: str, since: datetime
) -> dict[str, Any] | None:
    """The order the exchange holds under ``client_id``, if any."""
    params: list[tuple[str, str | int]] = [
        ("ticker", ticker),
        ("min_ts", int(since.timestamp())),
        ("limit", 1000),
    ]
    async for page in client.paginate("/portfolio/orders", params, "orders", auth=True):
        for raw in page:
            if raw.get("client_order_id") == client_id:
                return dict(raw)
    return None


async def _fills_for(
    client: KalshiClient, order_id: str, expected: Qty
) -> tuple[tuple[ExchangeFill, ...], str]:
    """The order's fills, waiting briefly if the fill feed lags the order response."""
    fills: tuple[ExchangeFill, ...] = ()
    for attempt in range(_FILL_ATTEMPTS):
        try:
            fills = await fetch_fills(client, order_id)
        except KalshiError as exc:
            return (
                fills,
                f"exchange reported {expected} filled but its fills could not be read: {exc}",
            )
        if sum(f.qty.raw for f in fills) == expected.raw:
            return fills, ""
        await client.pause(0.5 * (attempt + 1))
    got = Qty(sum(f.qty.raw for f in fills))
    return fills, f"exchange reported {expected} filled but listed fills for {got}"


async def _resolve(
    client: KalshiClient,
    order: Order,
    client_id: str,
    since: datetime,
    error: str,
    balance_unit: int,
) -> PlacedOrder:
    """An order without a clean result: find out whether the exchange has it."""
    try:
        found = await find_order(client, order.ticker, client_id, since)
    except KalshiError as exc:
        return PlacedOrder(
            order,
            client_id,
            None,
            error=f"{error}; lookup failed: {exc}",
            balance_unit=balance_unit,
        )
    if found is None:
        return PlacedOrder(order, client_id, None, error=error, balance_unit=balance_unit)
    order_id = str(found["order_id"])
    filled = Qty.parse(str(found.get("fill_count_fp") or "0.00"))
    fills, mismatch = await _fills_for(client, order_id, filled) if filled.raw else ((), "")
    return PlacedOrder(order, client_id, order_id, fills, mismatch, found, balance_unit)


async def place_orders(
    client: KalshiClient,
    orders: Sequence[Order],
    *,
    trade_id: str,
    phase: str,
    since: datetime,
    batch_size: int = 10,
    balance_unit: int = CENT_BALANCE_UNIT,
) -> tuple[PlacedOrder, ...]:
    """Send ``orders`` as immediate-or-cancel buys, in batches, and return what each did."""
    placed: list[PlacedOrder] = []
    seq = 0
    for start in range(0, len(orders), batch_size):
        chunk = orders[start : start + batch_size]
        ids = [client_order_id(trade_id, phase, seq + i) for i in range(len(chunk))]
        seq += len(chunk)
        body = {"orders": [order_request(o, cid) for o, cid in zip(chunk, ids, strict=True)]}
        results: list[dict[str, Any]] = []
        failure = ""
        try:
            payload = await client.post(BATCH_PATH, body, cost=len(chunk))
            results = [dict(r) for r in payload.get("orders") or []]
        except KalshiError as exc:
            failure = str(exc)
        by_id = {str(r.get("client_order_id")): r for r in results if r.get("client_order_id")}
        positional = len(results) == len(chunk)
        for index, (order, cid) in enumerate(zip(chunk, ids, strict=True)):
            result = by_id.get(cid) or (results[index] if positional else None)
            error = result.get("error") if result else None
            if result is None or error or not result.get("order_id"):
                reason = failure or (
                    f"order rejected: {error.get('message') or error.get('code')}"
                    if isinstance(error, dict)
                    else "no result for this order"
                )
                placed.append(await _resolve(client, order, cid, since, reason, balance_unit))
                continue
            order_id = str(result["order_id"])
            filled = Qty.parse(str(result.get("fill_count") or "0.00"))
            fills, mismatch = await _fills_for(client, order_id, filled) if filled.raw else ((), "")
            placed.append(PlacedOrder(order, cid, order_id, fills, mismatch, result, balance_unit))
    return tuple(placed)


async def fetch_balance(client: KalshiClient) -> Cash:
    payload = await client.get("/portfolio/balance", auth=True)
    return Cash.parse(str(payload["balance_dollars"]))


async def fetch_positions(client: KalshiClient, event_ticker: str | None) -> dict[str, int]:
    """Signed positions by market in raw 0.01-contract units: YES positive, NO negative."""
    positions: dict[str, int] = {}
    params: list[tuple[str, str | int]] = [("limit", 1000)]
    if event_ticker is not None:
        params.append(("event_ticker", event_ticker))
    async for page in client.paginate(
        "/portfolio/positions", params, "market_positions", auth=True
    ):
        for raw in page:
            positions[str(raw["ticker"])] = parse_scaled(
                str(raw["position_fp"]), QTY_DECIMALS, signed=True
            )
    return positions


async def fetch_settlements(client: KalshiClient, event_ticker: str) -> list[dict[str, Any]]:
    settlements: list[dict[str, Any]] = []
    params: list[tuple[str, str | int]] = [("event_ticker", event_ticker), ("limit", 1000)]
    async for page in client.paginate("/portfolio/settlements", params, "settlements", auth=True):
        settlements.extend(dict(raw) for raw in page)
    return settlements
