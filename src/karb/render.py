"""Terminal rendering for the CLI: opportunities, the universe, audits, explanations, JSON."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text

from karb.arb.detect import Detection
from karb.arb.opportunity import Opportunity, OpportunityTracker, Sighting
from karb.arb.screen import ScreenHit
from karb.core.fixed import Cash
from karb.exchange.client import ClientStats
from karb.market.book import OrderBook, Side
from karb.scanner.service import CycleReport, Universe
from karb.structure.classify import EventStructure, StructureKind, Tier

__all__ = [
    "audit_view",
    "explain_view",
    "opportunities_table",
    "opportunity_record",
    "status_line",
    "universe_view",
]


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds < 90:
        return f"{sign}{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{sign}{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{sign}{seconds / 3600:.0f}h"
    return f"{sign}{seconds / 86400:.0f}d"


def percent(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    if value > 99.99:
        return ">9999%"
    return f"{value:.{digits}%}"


def short_ticker(ticker: str, event_ticker: str) -> str:
    prefix = f"{event_ticker}-"
    return ticker[len(prefix) :] if ticker.startswith(prefix) else ticker


def basket_summary(opportunity: Opportunity) -> str:
    legs = opportunity.basket.legs
    if len(legs) <= 2:
        return " + ".join(
            f"{leg.side.value.upper()} {short_ticker(leg.ticker, opportunity.event_ticker)} "
            f"x{leg.order.qty.to_float():g}"
            for leg in legs
        )
    sides = Counter(leg.side.value.upper() for leg in legs)
    sizes = {leg.order.qty for leg in legs}
    size = f" x{next(iter(sizes)).to_float():g}" if len(sizes) == 1 else ""
    return " + ".join(f"{count} {side}" for side, count in sorted(sides.items())) + size


def opportunities_table(
    tracker: OpportunityTracker, *, now: datetime, show_unconfirmed: bool
) -> Table:
    table = Table(title="Structural arbitrage", header_style="bold")
    for name, justify in (
        ("#", "right"),
        ("Event", "left"),
        ("Kind", "left"),
        ("Tier", "left"),
        ("Basket", "left"),
        ("Cost", "right"),
        ("Fees", "right"),
        ("Guaranteed", "right"),
        ("Edge", "right"),
        ("APR", "right"),
        ("Expires", "right"),
        ("Seen", "right"),
        ("Skew", "right"),
    ):
        table.add_column(name, justify=justify)  # type: ignore[arg-type]
    rank = 0
    for sighting in tracker.live():
        confirmed = tracker.is_confirmed(sighting)
        if not (confirmed or show_unconfirmed):
            continue
        opportunity = sighting.opportunity
        rank += 1
        expires = (
            None if opportunity.expires is None else (opportunity.expires - now).total_seconds()
        )
        table.add_row(
            str(rank),
            opportunity.event_ticker,
            opportunity.kind.value,
            opportunity.tier.value,
            basket_summary(opportunity),
            opportunity.cost.dollars(),
            opportunity.basket.fees.dollars(),
            opportunity.guaranteed_pnl.dollars(),
            percent(opportunity.edge, 2),
            percent(opportunity.apr, 0),
            duration(expires),
            f"x{sighting.consecutive} {duration(sighting.age_seconds)}",
            f"{opportunity.snapshot_skew * 1000:.0f}ms",
            style=None if confirmed else "dim",
        )
    if rank == 0:
        qualifier = "" if show_unconfirmed else " confirmed"
        table.caption = (
            f"No{qualifier} opportunities -- the normal state of a well-arbitraged book."
        )
    return table


def status_line(report: CycleReport, stats: ClientStats, tracker: OpportunityTracker) -> Text:
    confirmed = sum(1 for sighting in tracker.live() if tracker.is_confirmed(sighting))
    issues = len(report.integrity) + len(report.fetch_errors)
    return Text(
        f"{report.finished_at:%H:%M:%S} UTC | {report.screened:,} events screened, "
        f"{len(report.hits)} screen hits | books confirmed for {len(report.detections)} events | "
        f"{confirmed} confirmed | requests {stats.requests:,} (retries {stats.retries:,}, "
        f"resets {stats.resets:,}, 429s {stats.throttled}) | integrity issues {issues}",
        style="dim",
    )


def universe_view(universe: Universe) -> Group:
    kinds = Counter(structure.kind for structure in universe.structures.values())
    tiers = Counter(tier for structure in universe.structures.values() for tier in structure.spaces)
    summary = Table(
        title=f"Universe at {universe.discovered_at:%Y-%m-%d %H:%M:%S} UTC", header_style="bold"
    )
    summary.add_column("Eligible structure")
    summary.add_column("Events", justify="right")
    summary.add_row("interval (strike ladders and ranges)", f"{kinds[StructureKind.INTERVAL]:,}")
    summary.add_row(
        "categorical (mutually exclusive outcomes)", f"{kinds[StructureKind.CATEGORICAL]:,}"
    )
    summary.add_row("  with a STRUCTURAL tier", f"{tiers[Tier.STRUCTURAL]:,}")
    summary.add_row("  with an ASSERTED tier", f"{tiers[Tier.ASSERTED]:,}")
    summary.add_row(
        "scannable event groups",
        f"{len(universe.structures):,} of {universe.groups_seen:,} "
        f"(from {universe.events_seen:,} open events)",
    )

    excluded = Table(title="Excluded", header_style="bold")
    excluded.add_column("Reason")
    excluded.add_column("Events", justify="right")
    excluded.add_column("Example")
    for reason, count in universe.exclusions.most_common():
        excluded.add_row(reason.value, f"{count:,}", universe.exclusion_examples.get(reason, ""))
    return Group(summary, excluded)


def audit_view(
    structure: EventStructure,
    tradeable: frozenset[str],
    books: dict[str, OrderBook],
    hits: list[ScreenHit],
    detection: Detection,
) -> Group:
    event = structure.event
    header = Text.assemble(
        (event.event_ticker, "bold"),
        f"  {event.title}\n",
        f"structure {structure.kind.value} | mutually_exclusive={event.mutually_exclusive} | "
        f"collateral_return_type={event.collateral_return_type or '-'} | fees "
        f"{structure.fees.fee_type.value} x{structure.fees.multiplier} ({structure.fees.source})",
    )

    markets = Table(title="Markets", header_style="bold")
    for column in (
        "Market",
        "YES resolves when",
        "Subtitle",
        "Trade",
        "Listing bid/ask",
        "Book bid/ask",
    ):
        markets.add_column(column)
    for market in event.markets:
        interval = structure.intervals.get(market.ticker)
        book = books.get(market.ticker)
        book_bid = book.best_bid(Side.YES) if book else None
        book_ask = book.best_ask(Side.YES) if book else None
        markets.add_row(
            short_ticker(market.ticker, event.event_ticker),
            f"X in {interval}" if interval is not None else "this outcome occurs",
            market.yes_sub_title[:40],
            "yes" if market.ticker in tradeable else "no",
            f"{market.quote.yes_bid or '-'} / {market.quote.yes_ask or '-'}",
            f"{book_bid.price if book_bid else '-'} / {book_ask.price if book_ask else '-'}",
        )

    spaces = Table(title="Outcome spaces", header_style="bold")
    for column in (
        "Tier",
        "Atoms",
        "Holes",
        "Overlaps",
        "Partition",
        "Grid",
        "LP guaranteed profit",
    ):
        spaces.add_column(column)
    for tier, space in structure.spaces.items():
        solution = detection.lp.get(tier)
        spaces.add_row(
            tier.value,
            str(space.size),
            str(len(space.holes())),
            str(len(space.overlaps())),
            "yes" if space.is_partition else "no",
            "-" if space.epsilon is None else str(space.epsilon),
            # x = 0 is always feasible, so a negative optimum is solver noise around zero.
            "not solved"
            if solution is None
            else f"${max(solution.profit, 0.0):,.6f} (before fee rounding)",
        )

    lines = [
        f"screen hit: {hit.tier.value} {hit.rule} (+{hit.gross_edge}/contract) {hit.detail}"
        for hit in hits
    ]
    lines = lines or ["screen hits: none"]
    lines.extend(f"integrity: {issue}" for issue in detection.integrity)
    if detection.opportunities:
        lines.extend(
            f"verified: {o.kind.value} {o.tier.value} guaranteed {o.guaranteed_pnl.dollars(4)}"
            for o in detection.opportunities
        )
    else:
        lines.append("verified opportunities: none")
    return Group(header, markets, spaces, Text("\n".join(lines)))


def explain_view(opportunity: Opportunity) -> Group:
    header = Text.assemble(
        (f"{opportunity.kind.value} {opportunity.tier.value}", "bold green"),
        f"  {opportunity.event_ticker}  id {opportunity.id}\n",
        f"guaranteed {opportunity.guaranteed_pnl.dollars(4)} on cost {opportunity.cost.dollars(4)} "
        f"(fees {opportunity.basket.fees.dollars(4)}) | edge {percent(opportunity.edge, 2)} | "
        f"APR {percent(opportunity.apr, 0)} | best case {opportunity.basket.best_pnl.dollars(4)}",
    )
    if opportunity.netted_capital is not None:
        header.append(
            f" | capital if collateral return enabled {opportunity.netted_capital.dollars(4)}"
        )

    legs = Table(title="Fills (taker, cheapest level first)", header_style="bold")
    for column in (
        "Market",
        "Side",
        "Price",
        "Qty",
        "Notional",
        "Trade fee",
        "Rounding",
        "Rebate",
        "Cash out",
    ):
        legs.add_column(column, justify="left" if column in ("Market", "Side") else "right")
    for leg in opportunity.basket.legs:
        for fill in leg.order.fills:
            legs.add_row(
                short_ticker(leg.ticker, opportunity.event_ticker),
                leg.side.value.upper(),
                str(fill.price),
                str(fill.qty),
                str(fill.notional),
                str(fill.trade_fee),
                str(fill.rounding_fee),
                str(fill.rebate),
                str(fill.cash_out),
            )

    by_payoff: dict[Cash, list[str]] = defaultdict(list)
    for atom, payoff in zip(opportunity.space.atoms, opportunity.basket.payoffs, strict=True):
        by_payoff[payoff].append(atom.label)
    payoffs = Table(title="Payoff in every outcome", header_style="bold")
    for column in ("Payout", "P&L", "Outcomes", "Examples"):
        payoffs.add_column(column, justify="left" if column == "Examples" else "right")
    for payoff in sorted(by_payoff):
        labels = by_payoff[payoff]
        examples = ", ".join(short_ticker(label, opportunity.event_ticker) for label in labels[:4])
        payoffs.add_row(
            payoff.dollars(4),
            (payoff - opportunity.cost).dollars(4),
            str(len(labels)),
            examples + (" ..." if len(labels) > 4 else ""),
        )
    return Group(header, legs, payoffs)


def opportunity_record(
    opportunity: Opportunity, sighting: Sighting | None = None, *, confirmed: bool | None = None
) -> dict[str, Any]:
    return {
        "id": opportunity.id,
        "event": opportunity.event_ticker,
        "series": opportunity.series_ticker,
        "title": opportunity.title,
        "kind": opportunity.kind.value,
        "tier": opportunity.tier.value,
        "observed_at": opportunity.observed_at.isoformat(),
        "expires": None if opportunity.expires is None else opportunity.expires.isoformat(),
        "cost": str(opportunity.cost),
        "fees": str(opportunity.basket.fees),
        "guaranteed_pnl": str(opportunity.guaranteed_pnl),
        "best_pnl": str(opportunity.basket.best_pnl),
        "edge": opportunity.edge,
        "apr": opportunity.apr,
        "netted_capital": None
        if opportunity.netted_capital is None
        else str(opportunity.netted_capital),
        "snapshot_skew_seconds": opportunity.snapshot_skew,
        "confirmed": confirmed,
        "consecutive_sightings": None if sighting is None else sighting.consecutive,
        "first_seen": None if sighting is None else sighting.first_seen.isoformat(),
        "legs": [
            {
                "ticker": leg.ticker,
                "side": leg.side.value,
                "qty": str(leg.order.qty),
                "cash_out": str(leg.order.cash_out),
                "fills": [
                    {
                        "price": str(fill.price),
                        "qty": str(fill.qty),
                        "trade_fee": str(fill.trade_fee),
                        "rounding_fee": str(fill.rounding_fee),
                        "rebate": str(fill.rebate),
                    }
                    for fill in leg.order.fills
                ],
            }
            for leg in opportunity.basket.legs
        ],
    }
