"""What recordings say: how often violations appear, how long they last, how much they hold, and
how much of that survives fees.

Lifetimes are *observed* lower bounds. An opportunity seen in one snapshot existed for at least
that instant; one seen in consecutive snapshots of its group lasted at least the time between the
first and last. An episode still present at its group's final observation is *censored*: it may
have lasted much longer.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from karb.arb.detect import DetectConfig
from karb.arb.lp import PROFIT_TOLERANCE
from karb.arb.opportunity import Opportunity
from karb.core.fixed import Cash
from karb.market.fees import CENTICENT_BALANCE_UNIT, FeeConfig, RoundingMode
from karb.store.codec import to_ns
from karb.store.database import LIVE_SOURCE, ObservationRow, OpportunityRow, RecordStore
from karb.store.replay import BookCache, replay_run

__all__ = [
    "Episode",
    "RunStatistics",
    "SensitivityRow",
    "SightingRecord",
    "build_episodes",
    "fee_scenarios",
    "fee_sensitivity",
    "quantile",
    "run_statistics",
    "sighting_from_opportunity",
    "sighting_from_row",
    "timelines",
]


@dataclass(frozen=True, slots=True)
class SightingRecord:
    cycle_no: int
    group_key: str
    opportunity_id: str
    kind: str
    tier: str
    observed_ns: int
    guaranteed_pnl: int
    cost: int
    contracts: int


def sighting_from_row(row: OpportunityRow) -> SightingRecord:
    return SightingRecord(
        cycle_no=row.cycle_no,
        group_key=row.group_key,
        opportunity_id=row.opportunity_id,
        kind=row.kind,
        tier=row.tier,
        observed_ns=row.observed_ns,
        guaranteed_pnl=row.guaranteed_pnl,
        cost=row.cost,
        contracts=row.contracts,
    )


def sighting_from_opportunity(
    cycle_no: int, group_key: str, opportunity: Opportunity
) -> SightingRecord:
    return SightingRecord(
        cycle_no=cycle_no,
        group_key=group_key,
        opportunity_id=opportunity.id,
        kind=opportunity.kind.value,
        tier=opportunity.tier.value,
        observed_ns=to_ns(opportunity.observed_at),
        guaranteed_pnl=opportunity.guaranteed_pnl.raw,
        cost=opportunity.cost.raw,
        contracts=max((leg.order.qty.raw for leg in opportunity.basket.legs), default=0),
    )


@dataclass(frozen=True, slots=True)
class Episode:
    """An unbroken run of sightings of one opportunity across its group's observations."""

    group_key: str
    opportunity_id: str
    kind: str
    tier: str
    first_cycle: int
    last_cycle: int
    first_ns: int
    last_ns: int
    sightings: int
    censored: bool
    """Still present at the group's last observation in the run."""
    best_guaranteed_pnl: int
    cost_at_best: int
    max_contracts: int

    @property
    def lifetime_seconds(self) -> float:
        return (self.last_ns - self.first_ns) / 1_000_000_000


def timelines(observations: Iterable[ObservationRow]) -> dict[str, list[int]]:
    """The cycles in which each group was observed, ascending."""
    result: dict[str, list[int]] = defaultdict(list)
    for row in observations:
        result[row.group_key].append(row.cycle_no)
    return {group: sorted(cycles) for group, cycles in result.items()}


def build_episodes(
    group_timelines: Mapping[str, Sequence[int]], sightings: Iterable[SightingRecord]
) -> list[Episode]:
    """Split sightings into episodes.

    An episode ends when its group is observed *without* the opportunity. A cycle in which the
    group was not observed at all says nothing, so it neither ends nor extends an episode.
    """
    present: dict[tuple[str, str], dict[int, SightingRecord]] = defaultdict(dict)
    for sighting in sightings:
        present[(sighting.group_key, sighting.opportunity_id)][sighting.cycle_no] = sighting

    episodes: list[Episode] = []
    for (group_key, _opportunity_id), by_cycle in sorted(present.items()):
        run: list[SightingRecord] = []
        for cycle_no in group_timelines.get(group_key, ()):
            found = by_cycle.get(cycle_no)
            if found is not None:
                run.append(found)
            elif run:
                episodes.append(_episode(run, censored=False))
                run = []
        if run:
            episodes.append(_episode(run, censored=True))
    return episodes


def _episode(run: Sequence[SightingRecord], *, censored: bool) -> Episode:
    best = max(run, key=lambda sighting: (sighting.guaranteed_pnl, -sighting.cost))
    return Episode(
        group_key=run[0].group_key,
        opportunity_id=run[0].opportunity_id,
        kind=best.kind,
        tier=best.tier,
        first_cycle=run[0].cycle_no,
        last_cycle=run[-1].cycle_no,
        first_ns=run[0].observed_ns,
        last_ns=run[-1].observed_ns,
        sightings=len(run),
        censored=censored,
        best_guaranteed_pnl=best.guaranteed_pnl,
        cost_at_best=best.cost,
        max_contracts=max(sighting.contracts for sighting in run),
    )


def quantile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank quantile; ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True, slots=True)
class RunStatistics:
    run_id: str
    source: str
    cycles: int
    rescreens: int
    observations: int
    groups: int
    duration_seconds: float
    screen_hits: list[tuple[str, str, int, int]]
    """(tier, rule, hits, distinct groups) from the live screens."""
    solver_positive: dict[str, int]
    verified_observations: int
    episodes: list[Episode]


def run_statistics(store: RecordStore, run_id: str, source: str = LIVE_SOURCE) -> RunStatistics:
    run = store.run(run_id)
    observations = store.observations(run_id)
    group_timelines = timelines(observations)
    cycles, rescreens = store.cycle_counts(run_id)
    last_ns = run.finished_ns
    if last_ns is None:
        last_ns = max((row.observed_ns for row in observations), default=run.started_ns)
    return RunStatistics(
        run_id=run_id,
        source=source,
        cycles=cycles,
        rescreens=rescreens,
        observations=len(observations),
        groups=len(group_timelines),
        duration_seconds=(last_ns - run.started_ns) / 1_000_000_000,
        screen_hits=store.screen_hit_counts(run_id),
        solver_positive=store.solver_positive(source, run_id, PROFIT_TOLERANCE),
        verified_observations=store.verified_observations(source, run_id),
        episodes=build_episodes(
            group_timelines, (sighting_from_row(row) for row in store.opportunities(source, run_id))
        ),
    )


@dataclass(frozen=True, slots=True)
class SensitivityRow:
    label: str
    config: DetectConfig
    replayed: int
    solver_positive_observations: int
    verified_observations: int
    episodes: int
    best_total: int
    """Sum over episodes of each episode's best guaranteed P&L, in $0.000001."""


def fee_scenarios(recorded: DetectConfig) -> list[tuple[str, DetectConfig]]:
    """The recorded fee model and the counterfactuals worth knowing about."""
    fees = recorded.fees
    return [
        ("as recorded", recorded),
        (
            "no fees, no rounding",
            replace(
                recorded,
                fees=FeeConfig(
                    taker_coefficient=Fraction(0), balance_unit=1, rounding_mode=fees.rounding_mode
                ),
                min_profit=Cash(1),
            ),
        ),
        (
            "half the taker coefficient",
            replace(recorded, fees=replace(fees, taker_coefficient=fees.taker_coefficient / 2)),
        ),
        (
            "direct member ($0.0001 balances)",
            replace(recorded, fees=replace(fees, balance_unit=CENTICENT_BALANCE_UNIT)),
        ),
        (
            "rebate accumulator",
            replace(recorded, fees=replace(fees, rounding_mode=RoundingMode.ACCUMULATOR)),
        ),
    ]


def fee_sensitivity(
    store: RecordStore,
    run_id: str,
    recorded: DetectConfig,
    *,
    scenarios: Sequence[tuple[str, DetectConfig]] | None = None,
    asserted_exhaustive: frozenset[str] = frozenset(),
) -> list[SensitivityRow]:
    """Replay the same recorded books under each fee scenario."""
    observations = store.observations(run_id)
    group_timelines = timelines(observations)
    books: BookCache = {}
    rows: list[SensitivityRow] = []
    for label, config in scenarios or fee_scenarios(recorded):
        outcome = replay_run(
            store,
            run_id,
            config,
            asserted_exhaustive=asserted_exhaustive,
            observations=observations,
            books=books,
        )
        episodes = build_episodes(
            group_timelines,
            (
                sighting_from_opportunity(cycle_no, group_key, opportunity)
                for cycle_no, group_key, opportunity in outcome.opportunities
            ),
        )
        rows.append(
            SensitivityRow(
                label=label,
                config=config,
                replayed=outcome.replayed,
                solver_positive_observations=outcome.solver_positive_observations,
                verified_observations=len(
                    {(cycle_no, group_key) for cycle_no, group_key, _ in outcome.opportunities}
                ),
                episodes=len(episodes),
                best_total=sum(episode.best_guaranteed_pnl for episode in episodes),
            )
        )
    return rows
