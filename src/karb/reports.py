"""Terminal reports for recorded research: runs, replays, statistics, fee sensitivity, history."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from rich.console import Group
from rich.table import Table
from rich.text import Text

from karb.core.fixed import Cash, format_scaled
from karb.history import HistoryScreen
from karb.render import duration
from karb.store.codec import from_ns
from karb.store.database import RunInfo
from karb.store.replay import ReplayComparison, ReplayOutcome
from karb.store.stats import Episode, RunStatistics, SensitivityRow, quantile

__all__ = ["history_view", "replay_view", "runs_table", "sensitivity_table", "stats_view"]


def _money(raw: float | None, decimals: int = 2) -> str:
    return "-" if raw is None else Cash(round(raw)).dollars(decimals)


def _columns(table: Table, columns: Sequence[tuple[str, str]]) -> None:
    for name, justify in columns:
        table.add_column(name, justify=justify)  # type: ignore[arg-type]


def runs_table(runs: Sequence[RunInfo]) -> Table:
    table = Table(title="Recorded runs", header_style="bold")
    _columns(
        table,
        (
            ("Run", "left"),
            ("Started (UTC)", "left"),
            ("Duration", "right"),
            ("Cycles", "right"),
            ("Observations", "right"),
            ("Live opportunities", "right"),
            ("Fee model", "left"),
        ),
    )
    for run in runs:
        detect = run.config.get("detect", {})
        table.add_row(
            run.run_id,
            f"{from_ns(run.started_ns):%Y-%m-%d %H:%M:%S}",
            "unfinished"
            if run.finished_ns is None
            else duration((run.finished_ns - run.started_ns) / 1_000_000_000),
            f"{run.cycles:,}",
            f"{run.observations:,}",
            f"{run.live_opportunities:,}",
            f"taker {detect.get('taker_coefficient', '?')}, {detect.get('rounding_mode', '?')}",
        )
    if not runs:
        table.caption = "No runs yet. Record one with: karb scan --record data/karb.duckdb"
    return table


def replay_view(outcome: ReplayOutcome, comparison: ReplayComparison | None) -> Group:
    observations = {(cycle_no, group) for cycle_no, group, _ in outcome.opportunities}
    tiers = ", ".join(
        f"{tier} {count:,}" for tier, count in sorted(outcome.solver_positive.items())
    )
    lines = [
        f"run {outcome.run_id}: replayed {outcome.replayed:,} of {outcome.observations:,} observations",
        f"LP positive before rounding: {tiers or 'none'}",
        f"verified after fees and rounding: {len(outcome.opportunities):,} sightings "
        f"in {len(observations):,} observations",
    ]
    lines.extend(f"skipped {count:,}: {reason}" for reason, count in outcome.skipped.most_common())
    if comparison is not None:
        if comparison.identical:
            lines.append("identical to the live run: same baskets, same exact costs and P&L")
        else:
            lines.append(
                f"differs from the live run: {len(comparison.missing)} missing, "
                f"{len(comparison.extra)} extra, {len(comparison.changed)} changed"
            )
    if outcome.replay_id is not None:
        lines.append(f"saved as source {outcome.replay_id}")

    parts: list[Table | Text] = [Text("\n".join(lines))]
    best = sorted(outcome.opportunities, key=lambda item: item[2].guaranteed_pnl, reverse=True)
    if best:
        table = Table(title="Best verified sightings", header_style="bold")
        _columns(
            table,
            (
                ("Cycle", "right"),
                ("Group", "left"),
                ("Kind", "left"),
                ("Tier", "left"),
                ("Cost", "right"),
                ("Guaranteed", "right"),
            ),
        )
        for cycle_no, group, opportunity in best[:15]:
            table.add_row(
                str(cycle_no),
                group,
                opportunity.kind.value,
                opportunity.tier.value,
                opportunity.cost.dollars(),
                opportunity.guaranteed_pnl.dollars(4),
            )
        parts.append(table)
    return Group(*parts)


def stats_view(stats: RunStatistics) -> Group:
    header = Text(
        f"run {stats.run_id} | source {stats.source} | {duration(stats.duration_seconds)} | "
        f"{stats.cycles:,} cycles ({stats.rescreens:,} with screens) | "
        f"{stats.observations:,} observations of {stats.groups:,} groups"
    )
    funnel = Table(title="Funnel", header_style="bold")
    _columns(funnel, (("Stage", "left"), ("Count", "right"), ("Groups", "right")))
    for tier, rule, hits, groups in stats.screen_hits:
        funnel.add_row(f"screen hit: {tier} {rule}", f"{hits:,}", f"{groups:,}")
    for tier, count in sorted(stats.solver_positive.items()):
        funnel.add_row(f"LP positive before rounding: {tier}", f"{count:,}", "")
    funnel.add_row(
        "verified after fees and rounding",
        f"{stats.verified_observations:,}",
        f"{len({episode.group_key for episode in stats.episodes}):,}",
    )
    funnel.add_row("distinct episodes", f"{len(stats.episodes):,}", "")
    if not stats.episodes:
        note = Text("No verified opportunities: nothing to measure lifetimes or capacity on.")
        return Group(header, funnel, note)

    by_label: dict[str, list[Episode]] = defaultdict(list)
    for episode in stats.episodes:
        by_label["all"].append(episode)
        by_label[f"{episode.kind} {episode.tier}"].append(episode)
    labels = sorted(by_label, key=lambda label: (label != "all", label))

    lifetime = Table(title="Lifetime (observed lower bound)", header_style="bold")
    _columns(
        lifetime,
        (
            ("Kind", "left"),
            ("Episodes", "right"),
            ("Still live at end", "right"),
            ("Median", "right"),
            ("P90", "right"),
            ("Longest", "right"),
            ("Median sightings", "right"),
        ),
    )
    capacity = Table(title="Capacity (best sighting of each episode)", header_style="bold")
    _columns(
        capacity,
        (
            ("Kind", "left"),
            ("Median guaranteed", "right"),
            ("P90", "right"),
            ("Largest", "right"),
            ("Sum", "right"),
            ("Median contracts", "right"),
        ),
    )
    for label in labels:
        episodes = by_label[label]
        lives = [episode.lifetime_seconds for episode in episodes]
        pnls = [float(episode.best_guaranteed_pnl) for episode in episodes]
        contracts = [episode.max_contracts / 100 for episode in episodes]
        lifetime.add_row(
            label,
            f"{len(episodes):,}",
            f"{sum(1 for episode in episodes if episode.censored):,}",
            duration(quantile(lives, 0.5)),
            duration(quantile(lives, 0.9)),
            duration(max(lives)),
            f"{quantile([float(episode.sightings) for episode in episodes], 0.5) or 0:g}",
        )
        capacity.add_row(
            label,
            _money(quantile(pnls, 0.5), 4),
            _money(quantile(pnls, 0.9), 4),
            _money(max(pnls), 4),
            _money(float(sum(episode.best_guaranteed_pnl for episode in episodes))),
            f"{quantile(contracts, 0.5) or 0:g}",
        )
    return Group(header, funnel, lifetime, capacity)


def sensitivity_table(rows: Sequence[SensitivityRow]) -> Table:
    table = Table(title="Fee sensitivity: replays of identical recorded books", header_style="bold")
    _columns(
        table,
        (
            ("Scenario", "left"),
            ("Taker coefficient", "right"),
            ("Balance unit", "right"),
            ("Rounding", "left"),
            ("Replayed", "right"),
            ("LP positive", "right"),
            ("Verified", "right"),
            ("Episodes", "right"),
            ("Sum of best guaranteed", "right"),
        ),
    )
    for row in rows:
        fees = row.config.fees
        unit = format_scaled(fees.balance_unit, 6).rstrip("0")
        table.add_row(
            row.label,
            f"{float(fees.taker_coefficient):g}",
            f"${unit}",
            fees.rounding_mode.value,
            f"{row.replayed:,}",
            f"{row.solver_positive_observations:,}",
            f"{row.verified_observations:,}",
            f"{row.episodes:,}",
            _money(float(row.best_total)),
        )
    return table


def history_view(results: Sequence[HistoryScreen], *, hours: float) -> Group:
    table = Table(
        title=f"Historical screen from one-minute candles, last {hours:g}h", header_style="bold"
    )
    _columns(
        table,
        (
            ("Group", "left"),
            ("Minutes", "right"),
            ("Minutes with hits", "right"),
            ("Share", "right"),
            ("Hits by rule (best gross edge per contract)", "left"),
        ),
    )
    for result in results:
        share = result.minutes_with_hits / result.minutes if result.minutes else 0.0
        rules = "; ".join(
            f"{rule}: {count:,} (+{result.best_edge[rule]})"
            for rule, count in result.hits_by_rule.most_common()
        )
        table.add_row(
            result.event_ticker,
            f"{result.minutes:,}",
            f"{result.minutes_with_hits:,}",
            f"{share:.1%}",
            rules or "-",
        )
    caveat = Text(
        "Top of book only, from minute closes with quiet minutes carried forward. A hit is a "
        "pre-fee necessary condition, never a verified opportunity: candles carry no depth.",
        style="dim",
    )
    return Group(table, caveat)
