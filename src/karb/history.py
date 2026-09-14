"""A coarse historical screen from Kalshi's one-minute candles (docs/data-caveats.md).

``GET /markets/candlesticks`` gives each market's YES bid and ask at the close of every minute
that saw activity. Aligning an event's markets on those minute boundaries -- carrying each
market's last close forward through quiet minutes -- gives one top-of-book snapshot per minute,
and the integer screens run on it unchanged.

It cannot verify anything. Candles carry no depth and no sizes, so a hit here is a pre-fee
necessary condition, never an opportunity; and a quote carried through a quiet minute is
assumed, not observed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from karb.arb.screen import ScreenHit, screen_event
from karb.core.fixed import Price, Qty
from karb.exchange.client import KalshiClient
from karb.market.model import Quote, listed_price
from karb.structure.classify import EventStructure
from karb.wire.candles import BatchCandlesWire, CandleWire

__all__ = [
    "MAX_CANDLES_PER_REQUEST",
    "MAX_TICKERS_PER_REQUEST",
    "HistoryScreen",
    "MinuteQuotes",
    "fetch_candles",
    "minute_snapshots",
    "screen_history",
]

MAX_CANDLES_PER_REQUEST: Final = 10_000
MAX_TICKERS_PER_REQUEST: Final = 100


async def fetch_candles(
    client: KalshiClient,
    tickers: Sequence[str],
    *,
    start_ts: int,
    end_ts: int,
    period_minutes: int = 1,
) -> dict[str, list[CandleWire]]:
    """Candles for ``tickers`` over ``[start_ts, end_ts]``, one per period, oldest first.

    Requests are split into time windows and ticker batches that stay under Kalshi's
    10,000-candle cap, and each asks for the latest candle before its window so a quiet market
    starts with a known quote.
    """
    if end_ts <= start_ts:
        raise ValueError("end_ts must be after start_ts")
    if period_minutes < 1:
        raise ValueError("period_minutes must be at least 1")
    period = 60 * period_minutes
    window = (MAX_CANDLES_PER_REQUEST // 2) * period
    by_market: dict[str, dict[int, CandleWire]] = {ticker: {} for ticker in tickers}
    for window_start in range(start_ts, end_ts, window):
        window_end = min(end_ts, window_start + window)
        per_market = (window_end - window_start) // period + 2
        chunk = max(1, min(MAX_TICKERS_PER_REQUEST, MAX_CANDLES_PER_REQUEST // per_market))
        for index in range(0, len(tickers), chunk):
            batch = tickers[index : index + chunk]
            payload = await client.get(
                "/markets/candlesticks",
                [
                    ("market_tickers", ",".join(batch)),
                    ("start_ts", window_start),
                    ("end_ts", window_end),
                    ("period_interval", period_minutes),
                    ("include_latest_before_start", "true"),
                ],
            )
            for market in BatchCandlesWire.model_validate(payload).markets:
                series = by_market.setdefault(market.market_ticker, {})
                for candle in market.candlesticks:
                    series[candle.end_period_ts] = candle
    return {ticker: [series[ts] for ts in sorted(series)] for ticker, series in by_market.items()}


@dataclass(frozen=True, slots=True)
class MinuteQuotes:
    ts: int
    """End of the minute, Unix seconds."""
    quotes: Mapping[str, Quote]


def _quote(candle: CandleWire) -> Quote:
    return Quote(
        yes_bid=listed_price(candle.yes_bid.close_dollars),
        yes_ask=listed_price(candle.yes_ask.close_dollars),
        yes_bid_size=Qty.ZERO,
        yes_ask_size=Qty.ZERO,
    )


def minute_snapshots(
    candles: Mapping[str, Sequence[CandleWire]], *, start_ts: int | None = None
) -> Iterator[MinuteQuotes]:
    """Top of book at every minute any market closed, with quiet markets carried forward.

    Candles before ``start_ts`` seed the carried quotes but yield no snapshot.
    """
    by_ticker = {
        ticker: {candle.end_period_ts: candle for candle in series}
        for ticker, series in candles.items()
    }
    timeline = sorted({ts for series in by_ticker.values() for ts in series})
    last: dict[str, Quote] = {}
    for ts in timeline:
        for ticker, series in by_ticker.items():
            candle = series.get(ts)
            if candle is not None:
                last[ticker] = _quote(candle)
        if start_ts is None or ts >= start_ts:
            yield MinuteQuotes(ts, dict(last))


@dataclass
class HistoryScreen:
    event_ticker: str
    minutes: int = 0
    minutes_with_hits: int = 0
    hits_by_rule: Counter[str] = field(default_factory=Counter)
    best_edge: dict[str, Price] = field(default_factory=dict)
    """Largest gross edge per contract seen for each rule."""
    first_ts: int | None = None
    last_ts: int | None = None
    examples: list[tuple[int, ScreenHit]] = field(default_factory=list)


def screen_history(
    structure: EventStructure, snapshots: Iterable[MinuteQuotes], *, examples: int = 5
) -> HistoryScreen:
    """Run the top-of-book screens over every minute snapshot of one event group."""
    result = HistoryScreen(structure.event.event_ticker)
    tickers = frozenset(structure.event.tickers)
    for snapshot in snapshots:
        result.minutes += 1
        if result.first_ts is None:
            result.first_ts = snapshot.ts
        result.last_ts = snapshot.ts
        # A market with no candle yet has no known quote, so it cannot be traded in this minute.
        quoted = frozenset(snapshot.quotes) & tickers
        hits = screen_event(structure, snapshot.quotes, quoted)
        if not hits:
            continue
        result.minutes_with_hits += 1
        for hit in hits:
            rule = f"{hit.tier.value}: {hit.rule}"
            result.hits_by_rule[rule] += 1
            if rule not in result.best_edge or hit.gross_edge > result.best_edge[rule]:
                result.best_edge[rule] = hit.gross_edge
            if len(result.examples) < examples:
                result.examples.append((snapshot.ts, hit))
    return result
