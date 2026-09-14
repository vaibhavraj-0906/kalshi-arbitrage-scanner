"""Replaying recordings through the current code (docs/decisions/ADR-0007).

A replay rebuilds every recorded observation from first principles. It re-parses the event
payload the exchange sent, splits and classifies it with today's rules, restores the order books
exactly as fetched, and runs the detector. Nothing the live run concluded is reused, so a replay
under the recorded configuration must reproduce the live run exactly, and a replay under any other
configuration is a clean counterfactual on identical market data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from karb import __version__
from karb.arb.detect import DetectConfig, EventSnapshot, detect
from karb.arb.opportunity import Opportunity
from karb.market.book import OrderBook
from karb.market.model import EventInfo, SeriesInfo, event_from_wire
from karb.store.codec import detect_config_to_json, from_ns, to_ns
from karb.store.database import LIVE_SOURCE, ObservationRow, RecordStore, new_id
from karb.structure.classify import (
    Classification,
    EventStructure,
    classify_event,
    split_by_participant,
)
from karb.wire.models import EventWire

__all__ = ["BookCache", "ReplayComparison", "ReplayOutcome", "compare_with_live", "replay_run"]

BookCache = dict[int, dict[str, OrderBook]]
"""Books by cycle, shareable across replays of one run so each cycle is read once."""


@dataclass
class ReplayOutcome:
    run_id: str
    config: DetectConfig
    replay_id: str | None = None
    observations: int = 0
    replayed: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    """Observations the current code no longer treats as scannable, by reason."""
    solver_positive: Counter[str] = field(default_factory=Counter)
    """Observations whose LP found a positive pre-rounding profit, by tier."""
    solver_positive_observations: int = 0
    opportunities: list[tuple[int, str, Opportunity]] = field(default_factory=list)
    """(cycle, group, opportunity) for every verified sighting."""


def replay_run(
    store: RecordStore,
    run_id: str,
    config: DetectConfig,
    *,
    asserted_exhaustive: frozenset[str] = frozenset(),
    save: bool = False,
    now: datetime | None = None,
    observations: Sequence[ObservationRow] | None = None,
    books: BookCache | None = None,
) -> ReplayOutcome:
    """Run the detector over every observation of ``run_id`` under ``config``.

    With ``save`` the replay's solver results and opportunities are written under a new replay
    id, so ``karb stats --source`` can analyse it like a live run.
    """
    rows = store.observations(run_id) if observations is None else observations
    cache: BookCache = {} if books is None else books
    outcome = ReplayOutcome(run_id, config)
    if save:
        moment = now or datetime.now(UTC)
        outcome.replay_id = new_id(moment)
        store.insert_replay(
            outcome.replay_id,
            run_id,
            to_ns(moment),
            __version__,
            {
                "detect": detect_config_to_json(config),
                "asserted_exhaustive": sorted(asserted_exhaustive),
            },
        )

    groups: dict[str, dict[str, EventInfo]] = {}
    classifications: dict[tuple[str, str, str, str], Classification] = {}
    for row in rows:
        outcome.observations += 1
        structure = _structure(store, row, groups, classifications, asserted_exhaustive, outcome)
        if structure is None:
            continue
        cycle_books = cache.get(row.cycle_no)
        if cycle_books is None:
            cycle_books = cache[row.cycle_no] = store.books(run_id, row.cycle_no)
        tradeable = frozenset(row.tradeable)
        snapshot = EventSnapshot(
            structure=structure,
            books={ticker: cycle_books[ticker] for ticker in tradeable if ticker in cycle_books},
            tradeable=tradeable,
            observed_at=from_ns(row.observed_ns),
            skew_seconds=row.skew_ns / 1_000_000_000,
        )
        detection = detect(snapshot, config)
        outcome.replayed += 1
        positive = [tier.value for tier, solution in detection.lp.items() if solution.found]
        outcome.solver_positive.update(positive)
        outcome.solver_positive_observations += 1 if positive else 0
        outcome.opportunities.extend(
            (row.cycle_no, row.group_key, opportunity) for opportunity in detection.opportunities
        )
        if outcome.replay_id is not None:
            store.insert_solver_results(
                outcome.replay_id,
                run_id,
                row.cycle_no,
                [
                    (row.group_key, tier.value, solution.status, solution.profit)
                    for tier, solution in detection.lp.items()
                ],
            )
            store.insert_opportunities(
                outcome.replay_id, run_id, row.cycle_no, row.group_key, detection.opportunities
            )
    return outcome


def _structure(
    store: RecordStore,
    row: ObservationRow,
    groups: dict[str, dict[str, EventInfo]],
    classifications: dict[tuple[str, str, str, str], Classification],
    asserted_exhaustive: frozenset[str],
    outcome: ReplayOutcome,
) -> EventStructure | None:
    key = (row.payload_hash, row.group_key, row.series_fee_type, row.series_fee_multiplier)
    classification = classifications.get(key)
    if classification is None:
        by_group = groups.get(row.payload_hash)
        if by_group is None:
            event = event_from_wire(EventWire.model_validate(store.payload(row.payload_hash)))
            by_group = {group.event_ticker: group for group in split_by_participant(event)}
            groups[row.payload_hash] = by_group
        group = by_group.get(row.group_key)
        if group is None:
            outcome.skipped["group no longer produced by participant splitting"] += 1
            return None
        multiplier = row.series_fee_multiplier
        series = SeriesInfo(
            row.series_ticker,
            "",
            "",
            row.series_fee_type,
            Decimal(multiplier) if multiplier else None,
        )
        classification = classify_event(group, series, asserted_exhaustive=asserted_exhaustive)
        classifications[key] = classification
    if classification.structure is None:
        reason = classification.exclusion.value if classification.exclusion else "unclassified"
        outcome.skipped[f"now excluded: {reason}"] += 1
        return None
    return classification.structure


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    """Replayed sightings against the live run's, keyed by (cycle, group, opportunity id)."""

    missing: frozenset[tuple[int, str, str]]
    extra: frozenset[tuple[int, str, str]]
    changed: frozenset[tuple[int, str, str]]
    """Same basket, different exact cost or guaranteed P&L."""

    @property
    def identical(self) -> bool:
        return not (self.missing or self.extra or self.changed)


def compare_with_live(store: RecordStore, outcome: ReplayOutcome) -> ReplayComparison:
    live = {
        (row.cycle_no, row.group_key, row.opportunity_id): (row.cost, row.guaranteed_pnl)
        for row in store.opportunities(LIVE_SOURCE, outcome.run_id)
    }
    replayed = {
        (cycle_no, group_key, opportunity.id): (
            opportunity.cost.raw,
            opportunity.guaranteed_pnl.raw,
        )
        for cycle_no, group_key, opportunity in outcome.opportunities
    }
    return ReplayComparison(
        missing=frozenset(live.keys() - replayed.keys()),
        extra=frozenset(replayed.keys() - live.keys()),
        changed=frozenset(
            key for key in live.keys() & replayed.keys() if live[key] != replayed[key]
        ),
    )
