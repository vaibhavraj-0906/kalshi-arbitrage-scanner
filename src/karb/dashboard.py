"""A self-contained HTML research report for one recorded run (docs/decisions/ADR-0009).

One file, no network: inline CSS, inline SVG, a few lines of script for tooltips. It opens in any
browser and can be attached, archived or published as is.

Chart rules follow the dataviz method the palette was validated with:
- one hue per job, and categorical hues never cycled;
- the ordinal blue ramp for funnel stages, re-stepped (not inverted) for dark mode;
- blue/red for gains and losses, with neutral grey totals;
- thin marks with 4px rounded data ends;
- every value directly labelled and repeated in a table view, so no tooltip gates a number.

Every label that came from the exchange (tickers, titles) is escaped on the way in.
"""

from __future__ import annotations

import html
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from itertools import pairwise
from typing import Final

from karb import __version__
from karb.arb.lp import PROFIT_TOLERANCE
from karb.core.fixed import Cash
from karb.render import duration
from karb.store.codec import detect_config_from_json, from_ns
from karb.store.database import LIVE_SOURCE, RecordStore, RunInfo, SessionRow, TradeRow
from karb.store.stats import (
    Episode,
    RunStatistics,
    SensitivityRow,
    fee_sensitivity,
    quantile,
    run_statistics,
)

__all__ = ["ReportData", "collect_report", "fee_coefficient", "render_report", "trade_number"]

_LIFETIME_BINS: Final = (
    ("one snapshot", 0.0),
    ("up to 10 s", 10.0),
    ("10-30 s", 30.0),
    ("30 s-1 min", 60.0),
    ("1-5 min", 300.0),
    ("5-15 min", 900.0),
    ("over 15 min", math.inf),
)


@dataclass(frozen=True, slots=True)
class ReportData:
    run: RunInfo
    stats: RunStatistics
    solver_positive: int
    cycle_gaps: list[float]
    """Seconds between consecutive cycle starts."""
    sensitivity: list[SensitivityRow] | None
    sessions: list[SessionRow]
    trades: list[TradeRow]
    generated_at: datetime


def collect_report(
    store: RecordStore, run_id: str, *, generated_at: datetime, sensitivity: bool = True
) -> ReportData:
    run = store.run(run_id)
    stats = run_statistics(store, run_id, LIVE_SOURCE)
    times = store.cycle_times(run_id)
    gaps = [(b[0] - a[0]) / 1e9 for a, b in pairwise(times)]
    rows: list[SensitivityRow] | None = None
    if sensitivity:
        recorded = detect_config_from_json(run.config["detect"])
        asserted = frozenset(run.config.get("asserted_exhaustive", []))
        rows = fee_sensitivity(store, run_id, recorded, asserted_exhaustive=asserted)
    sessions = [s for s in store.sessions() if s.run_id == run_id]
    ids = {s.session_id for s in sessions}
    trades = [t for t in store.trades() if t.session_id in ids]
    return ReportData(
        run=run,
        stats=stats,
        solver_positive=store.solver_positive_observations(LIVE_SOURCE, run_id, PROFIT_TOLERANCE),
        cycle_gaps=gaps,
        sensitivity=rows,
        sessions=sessions,
        trades=trades,
        generated_at=generated_at,
    )


# ---- building blocks ----------------------------------------------------------------------------


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _count(n: int) -> str:
    return f"{n:,}"


def _money(raw: int, decimals: int = 2) -> str:
    return Cash(raw).dollars(decimals)


def fee_coefficient(text: object) -> str:
    try:
        return f"{float(Fraction(str(text))):g}"
    except (ValueError, ZeroDivisionError):
        return "?"


def trade_number(trade: TradeRow) -> str:
    """Short and unique across sessions: the session's suffix and the trade's sequence."""
    return f"{trade.session_id[-6:]}/{trade.trade_id.rsplit('-', 1)[-1]}"


def _tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="tile-note">{_e(note)}</div>' if note else ""
    return (
        f'<div class="tile"><div class="tile-label">{_e(label)}</div>'
        f'<div class="tile-value">{_e(value)}</div>{note_html}</div>'
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], numeric_from: int = 1) -> str:
    head = "".join(
        f"<th{' class=num' if i >= numeric_from else ''}>{_e(h)}</th>"
        for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f"<td{' class=num' if i >= numeric_from else ''}>{_e(cell)}</td>"
            for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _figure(title: str, subtitle: str, chart: str, table: str) -> str:
    return (
        f'<figure class="card"><figcaption><h2>{_e(title)}</h2>'
        f'<p class="sub">{_e(subtitle)}</p></figcaption>{chart}'
        f"<details><summary>Table view</summary>{table}</details></figure>"
    )


def _empty(title: str, message: str) -> str:
    return f'<section class="card"><h2>{_e(title)}</h2><p class="empty">{_e(message)}</p></section>'


def _hbars(rows: Sequence[tuple[str, int, str, str]], *, aria: str) -> str:
    """Horizontal bars in HTML: (label, value, displayed value, colour class)."""
    peak = max((value for _, value, _, _ in rows), default=0) or 1
    items = []
    for label, value, shown, colour in rows:
        # The longest bar stops short of the track so its label is never pushed out of it.
        width = 0.0 if value <= 0 else max(0.6, 82 * value / peak)
        bar = f'<div class="bar {colour}" style="width:{width:.2f}%"></div>' if value > 0 else ""
        items.append(
            f'<div class="hrow" tabindex="0" data-value="{_e(shown)}" data-label="{_e(label)}">'
            f'<div class="hlabel">{_e(label)}</div>'
            f'<div class="htrack">{bar}<span class="hvalue">{_e(shown)}</span></div></div>'
        )
    return f'<div class="hbars" role="img" aria-label="{_e(aria)}">{"".join(items)}</div>'


def _column_path(x: float, width: float, top: float, bottom: float, round_top: bool) -> str:
    """A column from ``top`` to ``bottom`` (SVG y), with a 4px rounded data end."""
    r = min(4.0, (bottom - top) / 2, width / 2)
    if r <= 0:
        return f"M{x:.1f},{top:.1f}h{width:.1f}v{bottom - top:.1f}h{-width:.1f}Z"
    if round_top:
        return (
            f"M{x:.1f},{bottom:.1f}V{top + r:.1f}Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f}"
            f"H{x + width - r:.1f}Q{x + width:.1f},{top:.1f} {x + width:.1f},{top + r:.1f}"
            f"V{bottom:.1f}Z"
        )
    return (
        f"M{x:.1f},{top:.1f}V{bottom - r:.1f}Q{x:.1f},{bottom:.1f} {x + r:.1f},{bottom:.1f}"
        f"H{x + width - r:.1f}Q{x + width:.1f},{bottom:.1f} {x + width:.1f},{bottom - r:.1f}"
        f"V{top:.1f}Z"
    )


def _columns_svg(
    columns: Sequence[tuple[str, float, float, str, str]], *, aria: str, height: int = 220
) -> str:
    """Vertical columns from ``start`` to ``end`` value, labelled at the data end.

    Each column: (category, start, end, displayed value, colour class). A histogram passes
    start = 0; a waterfall passes the running total.
    """
    slot, bar_w, pad_x, pad_top, pad_bottom = 92.0, 24.0, 12.0, 26.0, 58.0
    width = pad_x * 2 + slot * len(columns)
    values = [v for _, start, end, _, _ in columns for v in (start, end)] + [0.0]
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    plot = height - pad_top - pad_bottom

    def y(value: float) -> float:
        return pad_top + (high - value) / span * plot

    parts = [
        f'<svg class="cols" width="{width:.0f}" height="{height}" viewBox="0 0 {width:.0f} {height}" '
        f'role="img" aria-label="{_e(aria)}">',
        f'<line class="baseline" x1="{pad_x}" x2="{width - pad_x:.0f}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>',
    ]
    for index, (label, start, end, shown, colour) in enumerate(columns):
        left = pad_x + index * slot
        x = left + (slot - bar_w) / 2
        top, bottom = sorted((y(start), y(end)))
        rising = end >= start
        if bottom - top >= 0.5:
            path = _column_path(x, bar_w, top, bottom, round_top=rising)
            parts.append(f'<path class="{colour}" d="{path}"/>')
        label_y = (top - 7) if rising else (bottom + 15)
        parts.append(
            f'<text class="cval" x="{x + bar_w / 2:.1f}" y="{label_y:.1f}">{_e(shown)}</text>'
            f'<text class="clab" x="{x + bar_w / 2:.1f}" y="{height - 10}">{_e(label)}</text>'
            f'<rect class="hit" tabindex="0" x="{left:.1f}" y="0" width="{slot:.1f}" '
            f'height="{height - 26}" data-value="{_e(shown)}" data-label="{_e(label)}"/>'
        )
    parts.append("</svg>")
    return f'<div class="scroll">{"".join(parts)}</div>'


# ---- sections -----------------------------------------------------------------------------------


def _header(data: ReportData) -> str:
    run, stats = data.run, data.stats
    detect = run.config.get("detect", {})
    started = from_ns(run.started_ns)
    gap = quantile(data.cycle_gaps, 0.5)
    facts = [
        f"run {run.run_id}",
        f"{started:%Y-%m-%d %H:%M} UTC",
        f"{duration(stats.duration_seconds)} long",
        f"{_count(stats.cycles)} cycles, median {duration(gap)} apart",
        f"taker fee {fee_coefficient(detect.get('taker_coefficient'))}, "
        f"{detect.get('rounding_mode', '?')} rounding",
        f"karb {run.karb_version}",
    ]
    return (
        "<header><h1>Kalshi structural arbitrage: research report</h1>"
        f'<p class="sub">{_e(" · ".join(facts))}</p></header>'
    )


def _kpis(data: ReportData) -> str:
    stats = data.stats
    final = [t for t in data.trades if t.realized_pnl is not None and t.status != "missed"]
    realized = sum(t.realized_pnl or 0 for t in final)
    tiles = [
        _tile("Event groups watched", _count(stats.groups)),
        _tile("Order-book snapshots", _count(stats.observations)),
        _tile("Solver-positive snapshots", _count(data.solver_positive), "before fee rounding"),
        _tile("Verified snapshots", _count(stats.verified_observations), "after fees and rounding"),
        _tile("Distinct opportunities", _count(len(stats.episodes))),
        _tile("Trades", _count(len(data.trades))),
        _tile(
            "Realized P&L",
            _money(realized) if final else "-",
            f"{len(final)} final trade(s)" if final else "nothing settled yet",
        ),
    ]
    return f'<section class="kpis">{"".join(tiles)}</section>'


def _funnel(data: ReportData) -> str:
    stats = data.stats
    stages = [
        ("Order-book snapshots", stats.observations),
        ("Solver-positive before rounding", data.solver_positive),
        ("Verified after fees and rounding", stats.verified_observations),
        ("Distinct opportunities", len(stats.episodes)),
        ("Trades", len(data.trades)),
    ]
    rows = [(label, n, _count(n), f"stage-{i + 1}") for i, (label, n) in enumerate(stages)]
    chart = _hbars(rows, aria="Funnel from snapshots to trades")
    table = _table(["Stage", "Count"], [(label, _count(n)) for label, n in stages])
    return _figure(
        "Where the edge goes",
        "Each stage is a strict subset of the one before. Snapshots are one event group's books "
        "in one cycle.",
        chart,
        table,
    )


def _sensitivity(rows: Sequence[SensitivityRow]) -> str:
    bars = [
        (row.label, row.verified_observations, _count(row.verified_observations), "series-1")
        for row in rows
    ]
    chart = _hbars(bars, aria="Verified snapshots under each fee scenario")
    table = _table(
        ["Scenario", "Solver-positive", "Verified", "Opportunities", "Sum of best guaranteed"],
        [
            (
                row.label,
                _count(row.solver_positive_observations),
                _count(row.verified_observations),
                _count(row.episodes),
                _money(row.best_total),
            )
            for row in rows
        ],
    )
    return _figure(
        "Fee sensitivity",
        "Verified snapshots when identical recorded books are replayed under each fee model.",
        chart,
        table,
    )


def _lifetimes(episodes: Sequence[Episode]) -> str:
    title = "How long opportunities lasted"
    if not episodes:
        return _empty(
            title,
            "No verified opportunity in this run, so there are no lifetimes to measure. That is the "
            "expected state of an efficient book: see the fee-sensitivity view for what fees hide.",
        )
    counts: Counter[str] = Counter()
    for episode in episodes:
        life = 0.0 if episode.sightings == 1 else episode.lifetime_seconds
        for label, upper in _LIFETIME_BINS:
            if (upper == 0.0 and life == 0.0) or (0.0 < life <= upper):
                counts[label] += 1
                break
    columns = [
        (label, 0.0, float(counts[label]), _count(counts[label]), "series-1")
        for label, _ in _LIFETIME_BINS
    ]
    censored = sum(1 for e in episodes if e.censored)
    chart = _columns_svg(columns, aria="Histogram of opportunity lifetimes")
    table = _table(
        ["Group", "Kind", "Sightings", "Lifetime", "Best guaranteed", "Still live at end"],
        [
            (
                e.group_key,
                f"{e.kind} {e.tier}",
                _count(e.sightings),
                duration(e.lifetime_seconds),
                _money(e.best_guaranteed_pnl, 4),
                "yes" if e.censored else "no",
            )
            for e in episodes
        ],
        numeric_from=2,
    )
    return _figure(
        title,
        f"Observed lower bounds: an opportunity is seen only when its group is confirmed. "
        f"{censored} of {len(episodes)} were still live when the run ended.",
        chart,
        table,
    )


def _trading(data: ReportData) -> str:
    title = "Trading: where the planned edge went"
    trades = data.trades
    if not data.sessions:
        return _empty(title, "This run was recorded without trading: see karb trade.")
    if not trades:
        return _empty(
            title,
            "Trading was on, but no opportunity cleared the budget and minimum-profit checks, "
            "so nothing was traded.",
        )
    final = [t for t in trades if t.realized_pnl is not None]
    open_trades = [t for t in trades if t.status == "open"]
    parts: list[str] = []
    if final:
        planned = sum(t.planned_pnl for t in final)
        execution = sum(t.worst_after_entry - t.planned_pnl for t in final)
        hedging = sum(t.worst_after_hedge - t.worst_after_entry for t in final)
        outcome = sum((t.realized_pnl or 0) - t.worst_after_hedge for t in final)
        realized = planned + execution + hedging + outcome
        steps = [("Planned", 0, planned, "total")]
        running = planned
        for label, delta in (
            ("Execution", execution),
            ("Hedging", hedging),
            ("Settlement", outcome),
        ):
            steps.append((label, running, running + delta, "gain" if delta >= 0 else "loss"))
            running += delta
        steps.append(("Realized", 0, realized, "total"))
        columns = [
            (label, start / 1e6, end / 1e6, _money(end - start if kind != "total" else end), kind)
            for label, start, end, kind in steps
        ]
        legend = (
            '<div class="legend"><span><i class="key total"></i>Total</span>'
            '<span><i class="key gain"></i>Adds P&amp;L</span>'
            '<span><i class="key loss"></i>Costs P&amp;L</span></div>'
        )
        chart = legend + _columns_svg(columns, aria="Waterfall from planned to realized P&L")
        table = _table(
            ["Step", "Amount"],
            [
                (label, _money(end - start if kind != "total" else end))
                for label, start, end, kind in steps
            ],
        )
        parts.append(
            _figure(
                title,
                f"{len(final)} final trade(s). Planned + execution + hedging + settlement = realized, "
                "exactly. Settlement can only add: a loss there means a contract was read wrong.",
                chart,
                table,
            )
        )
    else:
        parts.append(_empty(title, "No trade has settled yet: run karb settle later."))

    rows = [
        (
            trade_number(t),
            f"{from_ns(t.decided_ns):%m-%d %H:%M:%S}",
            t.group_key,
            f"{t.kind} {t.tier}",
            _money(t.planned_pnl),
            _money(t.worst_after_entry - t.planned_pnl),
            _money(t.worst_after_hedge - t.worst_after_entry),
            "-" if t.realized_pnl is None else _money(t.realized_pnl),
            f"{t.filled_contracts / t.planned_contracts:.0%}" if t.planned_contracts else "-",
            t.status + (" - MODEL VIOLATION" if t.model_violation else ""),
        )
        for t in trades
    ]
    guaranteed = sum(t.worst_after_hedge for t in open_trades)
    parts.append(
        '<section class="card"><h2>Trades</h2>'
        f'<p class="sub">{len(open_trades)} open, holding at least {_e(_money(guaranteed))} '
        "guaranteed until settlement.</p>"
        + '<div class="scroll">'
        + _table(
            [
                "#",
                "Decided (UTC)",
                "Group",
                "Kind",
                "Planned",
                "Execution",
                "Hedging",
                "Realized",
                "Filled",
                "Status",
            ],
            rows,
            numeric_from=4,
        )
        + "</div></section>"
    )
    return "".join(parts)


def _screens(stats: RunStatistics) -> str:
    if not stats.screen_hits:
        return _empty("Screen hits", "No top-of-book screen fired during this run.")
    return (
        '<section class="card"><h2>Screen hits</h2><p class="sub">Integer necessary conditions on '
        "listing quotes, counted each time listings refreshed. A hit only earns a book fetch.</p>"
        + _table(
            ["Tier", "Rule", "Hits", "Groups"],
            [
                (tier, rule, _count(hits), _count(groups))
                for tier, rule, hits, groups in stats.screen_hits
            ],
            numeric_from=2,
        )
        + "</section>"
    )


_METHOD: Final = """
<section class="card method"><h2>Method and caveats</h2><ul>
<li>Every opportunity is a basket whose exact, fee-rounded payout beats its cost in every outcome
the event can settle to: a linear programme over outcome atoms proposes it and integer arithmetic
verifies it (ADR-0003).</li>
<li>Fees: taker coefficient 0.07 times the series multiplier, rounded up per fill and aligned
against the trader; no rebates assumed unless stated (ADR-0002).</li>
<li>Lifetimes are lower bounds: groups are observed only when confirmed, every few seconds, over a
lossy public REST connection (ADR-0004).</li>
<li>Trades are immediate-or-cancel orders on Kalshi's demo exchange (mock funds) or a local
simulated exchange, priced with the fees the exchange charged; partial fills are repaired with the
same LP; settlement audits the contract reading (ADR-0008, ADR-0010).</li>
<li>Recordings made before ADR-0010 hold paper trades, simulated against fetched books.</li>
<li>Nothing in karb can trade real money: it signs requests for the demo exchange only.</li>
</ul></section>
"""

_STYLE: Final = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --gain: #2a78d6; --loss: #e34948; --total: #898781;
  --stage-1: #86b6ef; --stage-2: #5598e7; --stage-3: #2a78d6; --stage-4: #1c5cab; --stage-5: #104281;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --gain: #3987e5; --loss: #e66767; --total: #898781;
    --stage-1: #184f95; --stage-2: #256abf; --stage-3: #3987e5; --stage-4: #6da7ec; --stage-5: #9ec5f4;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --gain: #3987e5; --loss: #e66767; --total: #898781;
  --stage-1: #184f95; --stage-2: #256abf; --stage-3: #3987e5; --stage-4: #6da7ec; --stage-5: #9ec5f4;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 20px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 16px; margin: 0 0 2px; font-weight: 600; }
.sub { color: var(--ink-2); margin: 0 0 14px; }
header { margin-bottom: 20px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 10px;
  margin-bottom: 16px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 14px; }
.tile-label { color: var(--ink-2); font-size: 12px; }
.tile-value { font-size: 24px; font-weight: 600; margin-top: 2px; }
.tile-note { color: var(--muted); font-size: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 18px 20px; margin: 0 0 16px; }
figure.card { margin: 0 0 16px; }
.hbars { display: grid; gap: 6px; }
.hrow { display: grid; grid-template-columns: minmax(150px, 34%) 1fr; align-items: center;
  gap: 12px; padding: 3px 4px; border-radius: 6px; outline: none; }
.hrow:hover, .hrow:focus-visible { background: var(--grid); }
.hlabel { color: var(--ink-2); }
.htrack { display: flex; align-items: center; gap: 8px; min-height: 20px; }
.bar { height: 20px; border-radius: 0 4px 4px 0; flex: none; }
.hvalue { font-variant-numeric: tabular-nums; white-space: nowrap; }
.series-1 { background: var(--series-1); fill: var(--series-1); }
.stage-1 { background: var(--stage-1); } .stage-2 { background: var(--stage-2); }
.stage-3 { background: var(--stage-3); } .stage-4 { background: var(--stage-4); }
.stage-5 { background: var(--stage-5); }
.gain { fill: var(--gain); background: var(--gain); }
.loss { fill: var(--loss); background: var(--loss); }
.total { fill: var(--total); background: var(--total); }
.scroll { overflow-x: auto; }
svg.cols { display: block; }
svg .baseline { stroke: var(--axis); stroke-width: 1; }
svg .cval { fill: var(--ink); font-size: 12px; text-anchor: middle; font-variant-numeric: tabular-nums; }
svg .clab { fill: var(--ink-2); font-size: 12px; text-anchor: middle; }
svg .hit { fill: transparent; outline: none; }
svg .hit:hover, svg .hit:focus-visible { fill: var(--grid); fill-opacity: 0.35; }
.legend { display: flex; gap: 16px; color: var(--ink-2); font-size: 12px; margin-bottom: 6px; }
.key { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px;
  vertical-align: -1px; }
details { margin-top: 12px; }
summary { color: var(--ink-2); cursor: pointer; font-size: 13px; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
th { color: var(--ink-2); font-weight: 600; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.empty { color: var(--ink-2); margin: 6px 0 0; }
.method ul { margin: 8px 0 0; padding-left: 18px; color: var(--ink-2); }
.method li { margin: 4px 0; }
footer { color: var(--muted); font-size: 12px; margin-top: 24px; }
#tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); display: grid; gap: 2px; z-index: 10; }
#tip strong { font-size: 14px; }
#tip span { color: var(--ink-2); }
"""

_SCRIPT: Final = """
(() => {
  const tip = document.getElementById('tip');
  const show = (el, x, y) => {
    const value = document.createElement('strong');
    value.textContent = el.dataset.value;
    const label = document.createElement('span');
    label.textContent = el.dataset.label;
    tip.replaceChildren(value, label);
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(x + 14, window.innerWidth - w - 8) + 'px';
    tip.style.top = Math.max(8, y - h - 10) + 'px';
  };
  const hide = () => { tip.hidden = true; };
  for (const el of document.querySelectorAll('[data-value]')) {
    el.addEventListener('pointermove', (e) => show(el, e.clientX, e.clientY));
    el.addEventListener('pointerleave', hide);
    el.addEventListener('focus', () => {
      const r = el.getBoundingClientRect();
      show(el, r.left + r.width / 2, r.top);
    });
    el.addEventListener('blur', hide);
  }
})();
"""


def render_report(data: ReportData) -> str:
    sections = [
        _header(data),
        _kpis(data),
        _funnel(data),
        _sensitivity(data.sensitivity) if data.sensitivity is not None else "",
        _lifetimes(data.stats.episodes),
        _trading(data),
        _screens(data.stats),
        _METHOD,
    ]
    footer = (
        f"<footer>Generated {data.generated_at:%Y-%m-%d %H:%M} UTC by karb {_e(__version__)} "
        "from a local recording. Public market data only.</footer>"
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>karb report {_e(data.run.run_id)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{''.join(sections)}{footer}</main>"
        f'<div id="tip" role="tooltip" hidden></div><script>{_SCRIPT}</script></body></html>'
    )
