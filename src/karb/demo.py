"""An offline, deterministic tour of every feature (docs/guide.md).

``karb demo`` runs the real scanner, recorder, paper trader and settlement code against a small
simulated exchange, so each research command has something to show without a network connection
or a lucky day on Kalshi. Every number it produces is derived by hand in the guide.

The exchange lists three events:

DEMO-WINNER   three mutually exclusive outcomes whose YES bids sum to $1.20: an overround worth
              $7.48 on 50 contracts a leg. By the time the paper trader's orders arrive, outcome C's
              YES bids are gone, so NO on C cannot be bought: a half-filled basket to repair.
DEMO-LADDER   "above 10" offered at 0.50 while "above 20" is bid 0.60: a monotonicity violation worth
              $1.96 on 30 contracts.
DEMO-RANGE    three brackets priced consistently: a control that must produce nothing.

Settlement: B wins DEMO-WINNER, and the ladder's underlying settles at 25.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx

from karb.arb.detect import DetectConfig
from karb.core.clock import FakeClock
from karb.core.fixed import Cash
from karb.exchange.client import KalshiClient
from karb.paper.live import PaperTrader
from karb.paper.settle import SettleSummary, settle_open_trades
from karb.paper.trade import PaperConfig
from karb.scanner.service import CycleReport, ScanConfig, Scanner
from karb.store.database import RecordStore
from karb.store.recorder import Recorder

__all__ = ["DEMO_AT", "DemoExchange", "DemoSummary", "run_demo"]

DEMO_AT: Final = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_CLOSE: Final = "2026-03-01T00:00:00Z"
_BASE: Final = "https://demo.karb.invalid/trade-api/v2"

_WINNER: Final = ("DEMO-WINNER-A", "DEMO-WINNER-B", "DEMO-WINNER-C")
_RESULTS: Final = {
    "DEMO-WINNER-A": "no",
    "DEMO-WINNER-B": "yes",
    "DEMO-WINNER-C": "no",
    "DEMO-LADDER-T10": "yes",
    "DEMO-LADDER-T20": "yes",
    "DEMO-RANGE-LOW": "no",
    "DEMO-RANGE-MID": "no",
    "DEMO-RANGE-HIGH": "yes",
}


def _market(
    ticker: str,
    event: str,
    title: str,
    *,
    strike_type: str = "custom",
    floor: float | None = None,
    cap: float | None = None,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "event_ticker": event,
        "market_type": "binary",
        "status": "active",
        "title": title,
        "yes_sub_title": title,
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
        "close_time": _CLOSE,
        "latest_expiration_time": _CLOSE,
        "exchange_index": 0,
        "volume_24h_fp": "1000.00",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }


def _book(yes: str, yes_size: str, no: str, no_size: str) -> dict[str, list[list[str]]]:
    return {"yes_dollars": [[yes, yes_size]], "no_dollars": [[no, no_size]]}


class DemoExchange:
    """A mock of the public endpoints the scanner, trader and settler use."""

    def __init__(self) -> None:
        self.events = [
            {
                "event_ticker": "DEMO-WINNER",
                "series_ticker": "DEMO",
                "title": "Who wins the demo?",
                "mutually_exclusive": True,
                "collateral_return_type": "MECNET",
                "markets": [_market(t, "DEMO-WINNER", f"Outcome {t[-1]}") for t in _WINNER],
            },
            {
                "event_ticker": "DEMO-LADDER",
                "series_ticker": "DEMO",
                "title": "Demo index above...?",
                "mutually_exclusive": False,
                "collateral_return_type": "DIRECNET",
                "markets": [
                    _market(
                        "DEMO-LADDER-T10",
                        "DEMO-LADDER",
                        "Above 10",
                        strike_type="greater",
                        floor=10,
                    ),
                    _market(
                        "DEMO-LADDER-T20",
                        "DEMO-LADDER",
                        "Above 20",
                        strike_type="greater",
                        floor=20,
                    ),
                ],
            },
            {
                "event_ticker": "DEMO-RANGE",
                "series_ticker": "DEMO",
                "title": "Demo index range?",
                "mutually_exclusive": True,
                "collateral_return_type": "MECNET",
                "markets": [
                    _market("DEMO-RANGE-LOW", "DEMO-RANGE", "Below 10", strike_type="less", cap=10),
                    _market(
                        "DEMO-RANGE-MID",
                        "DEMO-RANGE",
                        "10 to 19.99",
                        strike_type="between",
                        floor=10,
                        cap=19.99,
                    ),
                    _market(
                        "DEMO-RANGE-HIGH",
                        "DEMO-RANGE",
                        "Above 19.99",
                        strike_type="greater",
                        floor=19.99,
                    ),
                ],
            },
        ]
        winner = _book("0.4000", "50.00", "0.5500", "50.00")
        bracket = _book("0.3000", "40.00", "0.6400", "40.00")
        self.books: dict[str, dict[str, list[list[str]]]] = {
            **{t: winner for t in _WINNER},
            "DEMO-LADDER-T10": _book("0.4500", "30.00", "0.5000", "30.00"),
            "DEMO-LADDER-T20": _book("0.6000", "30.00", "0.3500", "30.00"),
            "DEMO-RANGE-LOW": bracket,
            "DEMO-RANGE-MID": bracket,
            "DEMO-RANGE-HIGH": bracket,
        }

    def _orderbook(self, ticker: str, *, arriving: bool) -> dict[str, Any]:
        book = self.books[ticker]
        if arriving and ticker == "DEMO-WINNER-C":
            book = {**book, "yes_dollars": []}  # someone took every YES bid on C
        return {"ticker": ticker, "orderbook_fp": book}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/trade-api/v2", 1)[1]
        payload: object
        if endpoint == "/exchange/status":
            payload = {
                "exchange_active": True,
                "trading_active": True,
                "exchange_index_statuses": [
                    {"exchange_index": 0, "exchange_active": True, "trading_active": True}
                ],
            }
        elif endpoint == "/series":
            payload = {"series": [{"ticker": "DEMO", "fee_type": "quadratic", "fee_multiplier": 1}]}
        elif endpoint == "/events":
            payload = {"events": self.events, "cursor": ""}
        elif endpoint == "/markets/orderbooks":
            wanted = request.url.params.get_list("tickers")
            # Only the paper trader asks for nothing but one event's markets.
            arriving = set(wanted) <= set(_WINNER)
            payload = {"orderbooks": [self._orderbook(t, arriving=arriving) for t in wanted]}
        elif endpoint == "/markets":
            tickers = request.url.params.get("tickers", "").split(",")
            payload = {
                "markets": [
                    {
                        "ticker": t,
                        "event_ticker": t.rsplit("-", 1)[0],
                        "status": "finalized",
                        "result": _RESULTS[t],
                        "settlement_value_dollars": "1.0000" if _RESULTS[t] == "yes" else "0.0000",
                        "settlement_ts": _CLOSE,
                    }
                    for t in tickers
                    if t in _RESULTS
                ],
                "cursor": "",
            }
        else:
            return httpx.Response(404, content=b"not part of the demo")
        return httpx.Response(200, content=json.dumps(payload).encode())


@dataclass(frozen=True, slots=True)
class DemoSummary:
    run_id: str
    paper_id: str
    cycles: int
    trades: int
    settlement: SettleSummary

    @property
    def realized(self) -> Cash:
        return self.settlement.realized


async def run_demo(path: Path) -> DemoSummary:
    """Scan the demo exchange three times, paper-trade what it finds, and settle."""
    clock = FakeClock(DEMO_AT)

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    exchange = DemoExchange()

    def client() -> KalshiClient:
        return KalshiClient(
            base_url=_BASE,
            transport=httpx.MockTransport(exchange),
            clock=clock,
            sleep=sleep,
            rng=random.Random(0),
        )

    with RecordStore(path) as store:
        recorder = Recorder(store, clock)
        config = ScanConfig(confirmations=3)
        async with client() as scanning:
            scanner = Scanner(scanning, config, sleep=sleep, record_payloads=True)
            run_id = recorder.start_run(config)
            trader = PaperTrader(
                scanning,
                store,
                run_id=run_id,
                detect_config=DetectConfig(),
                config=PaperConfig(),
                sleep=sleep,
            )

            def handle(report: CycleReport) -> None:
                cycle_no = recorder.record_cycle(
                    report, payloads=scanner.event_payloads, series=scanner.series
                )
                trader.consider(report, cycle_no)

            await scanner.run_once(on_cycle=handle)
            await trader.drain()
        recorder.finish_run()
        clock.advance(60 * 24 * 3600)  # ...and two months later, the markets settle
        async with client() as settling:
            settlement = await settle_open_trades(store, settling, paper_id=trader.paper_id)
        return DemoSummary(
            run_id, trader.paper_id, recorder.cycles, len(trader.outcomes), settlement
        )
