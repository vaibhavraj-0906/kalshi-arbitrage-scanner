"""An offline, deterministic tour of every feature (docs/guide.md).

``karb demo`` runs the real scanner, recorder, trader and settlement code against a small
simulated exchange, so each command has something to show without a network connection, an
account, or a lucky day on Kalshi. The trader sends real signed orders; the simulated exchange
verifies the signatures, matches the orders against its books and charges Kalshi's fees
(``karb.trading.simulator``). Every number it produces is derived by hand in the guide.

The exchange lists three events:

DEMO-WINNER   three mutually exclusive outcomes whose YES bids sum to $1.20: an overround worth
              $7.48 on 50 contracts a leg. The moment the trader's first order lands, someone takes
              every YES bid on C, so NO on C cannot be bought: a half-filled basket to repair.
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
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from karb.arb.detect import DetectConfig
from karb.core.clock import FakeClock
from karb.core.fixed import Cash
from karb.exchange.client import KalshiClient
from karb.market.fees import DEFAULT_TAKER_COEFFICIENT
from karb.scanner.service import CycleReport, ScanConfig, Scanner
from karb.store.database import RecordStore
from karb.store.recorder import Recorder
from karb.trading.auth import Credentials
from karb.trading.engine import Trader
from karb.trading.plan import TradeConfig
from karb.trading.settle import SettleSummary, settle_open_trades
from karb.trading.simulator import SimulatedDesk

__all__ = ["DEMO_AT", "DemoExchange", "DemoSummary", "run_demo"]

DEMO_AT: Final = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_CLOSE: Final = "2026-03-01T00:00:00Z"
_HOST: Final = "demo.karb.invalid"
_BASE: Final = f"https://{_HOST}/trade-api/v2"
_KEY_ID: Final = "karb-offline-demo"

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
    """The public endpoints the scanner and settler use, and a simulated trading desk."""

    def __init__(self, clock: FakeClock, credentials: Credentials) -> None:
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
        # Every market gets its own book: orders consume size from them.
        self.books: dict[str, dict[str, list[list[str]]]] = {
            **{t: _book("0.4000", "50.00", "0.5500", "50.00") for t in _WINNER},
            "DEMO-LADDER-T10": _book("0.4500", "30.00", "0.5000", "30.00"),
            "DEMO-LADDER-T20": _book("0.6000", "30.00", "0.3500", "30.00"),
            **{
                t: _book("0.3000", "40.00", "0.6400", "40.00")
                for t in ("DEMO-RANGE-LOW", "DEMO-RANGE-MID", "DEMO-RANGE-HIGH")
            },
        }
        self._raced = False
        self.desk = SimulatedDesk(
            ladders=self.books,
            clock=clock,
            fee_rate=lambda _ticker: Fraction(DEFAULT_TAKER_COEFFICIENT),
            public_key=credentials.public_key(),
            key_id=credentials.key_id,
            results=_RESULTS,
            on_order=self._race,
        )

    def _race(self, ticker: str) -> None:
        """The first order on DEMO-WINNER lands just after someone takes every YES bid on C."""
        if ticker in _WINNER and not self._raced:
            self._raced = True
            self.books["DEMO-WINNER-C"]["yes_dollars"] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/trade-api/v2", 1)[1]
        if endpoint.startswith("/portfolio/"):
            return self.desk.handle(request, endpoint)
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
            payload = {"orderbooks": [{"ticker": t, "orderbook_fp": self.books[t]} for t in wanted]}
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
    session_id: str
    cycles: int
    trades: int
    settlement: SettleSummary
    balance: Cash
    halted: str | None

    @property
    def realized(self) -> Cash:
        return self.settlement.realized


async def run_demo(path: Path) -> DemoSummary:
    """Scan the simulated exchange three times, trade what it finds, and settle."""
    clock = FakeClock(DEMO_AT)

    async def sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    credentials = Credentials(_KEY_ID, Ed25519PrivateKey.generate())
    exchange = DemoExchange(clock, credentials)

    def client(signing: Credentials | None = None) -> KalshiClient:
        return KalshiClient(
            base_url=_BASE,
            transport=httpx.MockTransport(exchange),
            clock=clock,
            sleep=sleep,
            rng=random.Random(0),
            credentials=signing,
            sign_hosts=frozenset({_HOST}),
        )

    with RecordStore(path) as store:
        recorder = Recorder(store, clock)
        config = ScanConfig(confirmations=3)
        async with client() as scanning, client(credentials) as trading:
            scanner = Scanner(scanning, config, sleep=sleep, record_payloads=True)
            run_id = recorder.start_run(config)
            trader = Trader(
                trading,
                store,
                run_id=run_id,
                detect_config=DetectConfig(),
                config=TradeConfig(),
                environment="simulated",
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
        exchange.desk.settle_all()
        async with client(credentials) as settling:
            settlement = await settle_open_trades(store, settling, session_id=trader.session_id)
        return DemoSummary(
            run_id,
            trader.session_id,
            recorder.cycles,
            len(trader.outcomes),
            settlement,
            exchange.desk.total_cash(),
            trader.halted,
        )
