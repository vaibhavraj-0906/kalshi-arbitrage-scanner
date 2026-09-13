"""The full offline pipeline over recorded Kalshi data, reduced to a deterministic summary.

Regenerate the expected file after an intentional change with:

    uv run python -m tests.golden.regenerate
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.arb.screen import screen_event
from karb.exchange.endpoints import trading_shards
from karb.structure.classify import classify_event, tradeable_tickers
from karb.wire.models import ExchangeStatusWire
from tests.support import captured_at, fixture_books, fixture_event, fixture_series, load_fixture

EXPECTED = Path(__file__).parent / "expected_scan.json"

CASES: list[tuple[str, str | None]] = [
    ("events_KXINX.json", "orderbooks_KXINX.json"),
    ("events_KXBTCD.json", "orderbooks_KXBTCD.json"),
    ("event_KXNEXTDNCCHAIR-45.json", "orderbooks_KXNEXTDNCCHAIR-45.json"),
    ("event_KXELONMARS-99.json", None),
]


def scan_summary() -> dict[str, Any]:
    shards = trading_shards(ExchangeStatusWire.model_validate(load_fixture("exchange_status.json")))
    series = fixture_series()
    at = captured_at()
    config = DetectConfig()
    events: dict[str, Any] = {}
    for event_file, books_file in CASES:
        event = fixture_event(event_file)
        result = classify_event(event, series.get(event.series_ticker))
        entry: dict[str, Any] = {
            "markets": len(event.markets),
            "mutually_exclusive": event.mutually_exclusive,
            "exclusion": None if result.exclusion is None else result.exclusion.value,
        }
        structure = result.structure
        if structure is not None and books_file is not None:
            tradeable = tradeable_tickers(event, now=at, trading_shards=shards)
            quotes = {market.ticker: market.quote for market in event.markets}
            hits = screen_event(structure, quotes, tradeable)
            detection = detect(
                EventSnapshot(structure, fixture_books(books_file), tradeable, at), config
            )
            entry.update(
                {
                    "kind": structure.kind.value,
                    "fees": f"{structure.fees.fee_type.value} x{structure.fees.multiplier}",
                    "tradeable": len(tradeable),
                    "spaces": {
                        tier.value: {
                            "atoms": space.size,
                            "holes": len(space.holes()),
                            "overlaps": len(space.overlaps()),
                            "partition": space.is_partition,
                            "grid": None if space.epsilon is None else str(space.epsilon),
                        }
                        for tier, space in structure.spaces.items()
                    },
                    "screen_hits": sorted(f"{hit.tier.value}: {hit.rule}" for hit in hits),
                    "lp_found": {
                        tier.value: solution.found for tier, solution in detection.lp.items()
                    },
                    "integrity": list(detection.integrity),
                    "opportunities": [
                        {
                            "id": opportunity.id,
                            "kind": opportunity.kind.value,
                            "tier": opportunity.tier.value,
                            "legs": [
                                f"{leg.side.value} {leg.ticker} x{leg.order.qty}"
                                for leg in opportunity.basket.legs
                            ],
                            "cost": str(opportunity.cost),
                            "guaranteed_pnl": str(opportunity.guaranteed_pnl),
                        }
                        for opportunity in detection.opportunities
                    ],
                }
            )
        events[event.event_ticker] = entry
    return {"captured_at": at.isoformat(), "events": events}
