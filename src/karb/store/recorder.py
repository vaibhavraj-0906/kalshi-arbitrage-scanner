"""Recording scanner cycles as they happen (docs/decisions/ADR-0007)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from karb import __version__
from karb.core.clock import Clock
from karb.market.book import OrderBook
from karb.market.model import SeriesInfo
from karb.scanner.service import CycleReport, ScanConfig
from karb.store.codec import detect_config_to_json, seconds_to_ns, to_ns
from karb.store.database import LIVE_SOURCE, ObservationRow, RecordStore, new_id

__all__ = ["Recorder", "RecordingError", "scan_config_to_json"]


class RecordingError(RuntimeError):
    """A cycle cannot be recorded faithfully, so none of it is recorded."""


def scan_config_to_json(config: ScanConfig) -> dict[str, Any]:
    return {
        "detect": detect_config_to_json(config.detect),
        "min_time_to_close_seconds": config.tradeability.min_time_to_close.total_seconds(),
        "series": list(config.series),
        "max_event_pages": config.max_event_pages,
        "asserted_exhaustive": sorted(config.asserted_exhaustive),
        "watchlist_size": config.watchlist_size,
        "confirm_all": config.confirm_all,
        "confirmations": config.confirmations,
        "discovery_interval": config.discovery_interval,
        "screen_interval": config.screen_interval,
        "confirm_interval": config.confirm_interval,
    }


class Recorder:
    """Writes every cycle: the books the scanner saw, the events behind them, what it found.

    Each cycle is one transaction, so an interrupted recording always ends on a cycle boundary.
    """

    def __init__(self, store: RecordStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.run_id: str | None = None
        self.cycles = 0
        self._digests: dict[str, tuple[str, str]] = {}
        self._stored: set[str] = set()

    def start_run(self, config: ScanConfig) -> str:
        if self.run_id is not None:
            raise RecordingError(f"run {self.run_id} is already being recorded")
        now = self.clock.now()
        self.run_id = new_id(now)
        self.store.insert_run(self.run_id, to_ns(now), __version__, scan_config_to_json(config))
        return self.run_id

    def record_cycle(
        self,
        report: CycleReport,
        *,
        payloads: Mapping[str, str],
        series: Mapping[str, SeriesInfo],
    ) -> int:
        """Record one confirmation cycle. ``payloads`` maps event tickers to structural JSON."""
        run_id = self._run_id()
        cycle_no = self.cycles + 1
        recorded_ns = to_ns(self.clock.now())

        observations: list[ObservationRow] = []
        new_payloads: dict[str, tuple[str, str]] = {}
        books: dict[str, OrderBook] = {}
        for group_key, snapshot in sorted(report.snapshots.items()):
            event = snapshot.structure.event
            parent = group_key.split("#", 1)[0]
            payload_json = payloads.get(parent)
            if payload_json is None:
                raise RecordingError(
                    f"no event payload retained for {parent}: "
                    "construct the Scanner with record_payloads=True"
                )
            digest = self._digest(parent, payload_json)
            if digest not in self._stored:
                new_payloads[digest] = (parent, payload_json)
            fees = series.get(event.series_ticker)
            observations.append(
                ObservationRow(
                    run_id=run_id,
                    cycle_no=cycle_no,
                    group_key=group_key,
                    event_ticker=parent,
                    series_ticker=event.series_ticker,
                    payload_hash=digest,
                    series_fee_type="" if fees is None else fees.fee_type,
                    series_fee_multiplier=(
                        ""
                        if fees is None or fees.fee_multiplier is None
                        else str(fees.fee_multiplier)
                    ),
                    observed_ns=to_ns(snapshot.observed_at),
                    skew_ns=seconds_to_ns(snapshot.skew_seconds),
                    tradeable=tuple(sorted(snapshot.tradeable)),
                )
            )
            books.update(snapshot.books)

        with self.store.transaction():
            self.store.insert_cycle(
                run_id,
                cycle_no,
                to_ns(report.started_at),
                to_ns(report.finished_at),
                rescreened=report.rescreened,
                screened=report.screened,
                untradeable=report.untradeable,
                targets=len(report.targets),
                fetch_errors=len(report.fetch_errors),
            )
            for digest, (parent, payload_json) in new_payloads.items():
                self.store.insert_payload(digest, parent, recorded_ns, payload_json)
            self.store.insert_observations(observations)
            self.store.insert_books(run_id, cycle_no, books)
            if report.rescreened:
                # Screens only re-run when listings refresh; recording them every cycle would
                # count one hit many times.
                self.store.insert_screen_hits(
                    run_id,
                    cycle_no,
                    [
                        (group_key, hit.tier.value, hit.rule, hit.gross_edge.raw, hit.detail)
                        for group_key, hits in sorted(report.hits.items())
                        for hit in hits
                    ],
                )
            for group_key, detection in sorted(report.detections.items()):
                self.store.insert_solver_results(
                    LIVE_SOURCE,
                    run_id,
                    cycle_no,
                    [
                        (group_key, tier.value, solution.status, solution.profit)
                        for tier, solution in detection.lp.items()
                    ],
                )
                self.store.insert_opportunities(
                    LIVE_SOURCE, run_id, cycle_no, group_key, detection.opportunities
                )
        self._stored.update(new_payloads)
        self.cycles = cycle_no
        return cycle_no

    def finish_run(self) -> None:
        self.store.finish_run(self._run_id(), to_ns(self.clock.now()))

    def _run_id(self) -> str:
        if self.run_id is None:
            raise RecordingError("start_run() must be called before recording")
        return self.run_id

    def _digest(self, parent: str, payload_json: str) -> str:
        # Discovery builds a fresh string per event, so identity says whether rehashing is needed.
        cached = self._digests.get(parent)
        if cached is not None and cached[0] is payload_json:
            return cached[1]
        digest = hashlib.sha256(payload_json.encode()).hexdigest()
        self._digests[parent] = (payload_json, digest)
        return digest
