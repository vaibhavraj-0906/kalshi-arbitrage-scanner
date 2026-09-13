"""Which events the scanner can reason about -- and exactly why the rest are excluded.

Classification is static: it depends on an event's markets, strikes and fees, not on the
clock. Whether a market can be traded *right now* is a separate, per-scan question
(``tradeable_tickers``), because it changes minute to minute while structure does not.

Strike reasoning is valid only across markets that settle off *one* value at *one* time, and
Kalshi's strike fields do not guarantee that. Sports events list many players' ladders side by
side; time-indexed questions reuse strike fields for deadlines. Treating those as one ladder
produced hundreds of phantom "arbitrages" on live data (docs/data-caveats.md), so an interval
event is only accepted when its markets demonstrably share an underlying.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from karb.market.fees import FeeSchedule, resolve_fee_schedule
from karb.market.model import EventInfo, MarketInfo, SeriesInfo
from karb.structure.intervals import (
    INTERVAL_STRIKE_TYPES,
    AtomKind,
    Interval,
    OutcomeSpace,
    StructureError,
    categorical_space,
    interval_space,
    structural_space,
    yes_interval,
)

__all__ = [
    "DEFAULT_TRADEABILITY",
    "Classification",
    "EventStructure",
    "Exclusion",
    "StructureKind",
    "Tier",
    "TradeabilityRules",
    "classify_event",
    "split_by_participant",
    "tradeable_tickers",
]

_INTERVAL_TYPE_NAMES: Final = frozenset(kind.value for kind in INTERVAL_STRIKE_TYPES)
_UNSUPPORTED_TYPE_NAMES: Final = frozenset({"functional", "structured"})


class StructureKind(StrEnum):
    INTERVAL = "interval"
    CATEGORICAL = "categorical"


class Tier(StrEnum):
    """What an opportunity's guarantee rests on (docs/decisions/ADR-0003).

    LOGICAL     holds for every outcome the event could possibly settle to
    STRUCTURAL  also assumes the settlement value lies on the grid the strikes are written on
    ASSERTED    also assumes a categorical event's listed outcomes are exhaustive, on the
                user's say-so
    """

    LOGICAL = "LOGICAL"
    STRUCTURAL = "STRUCTURAL"
    ASSERTED = "ASSERTED"


class Exclusion(StrEnum):
    MULTIVARIATE = "multivariate combo event"
    SINGLE_MARKET = "fewer than two markets"
    NON_BINARY = "has non-binary markets"
    RESOLVED = "a market already resolved YES"
    MIXED_PARTICIPANTS = "strike markets about different participants"
    STAGGERED_SETTLEMENT = "strike markets settle at different times"
    INVALID_STRIKES = "strike fields do not form valid intervals"
    DUPLICATE_STRIKES = "two markets share one YES-interval"
    AMBIGUOUS_BOUNDARIES = "adjacent strikes share a boundary point"
    CONTRADICTORY = "mutually_exclusive contradicts overlapping strikes"
    UNSUPPORTED_STRIKES = "functional or structured strikes"
    NO_STRUCTURE = "no exploitable structure"
    FEES = "fee schedule not modelled"


@dataclass(frozen=True, slots=True)
class EventStructure:
    event: EventInfo
    fees: FeeSchedule
    kind: StructureKind
    spaces: Mapping[Tier, OutcomeSpace]
    """Outcome spaces by tier, weakest assumption first. LOGICAL is always present."""
    intervals: Mapping[str, Interval]
    """YES-intervals by ticker. Empty for categorical events."""


@dataclass(frozen=True, slots=True)
class Classification:
    event: EventInfo
    structure: EventStructure | None
    exclusion: Exclusion | None
    detail: str = ""


def _all_interval_typed(markets: Sequence[MarketInfo]) -> bool:
    return all(
        market.strike_type is not None and market.strike_type in _INTERVAL_TYPE_NAMES
        for market in markets
    )


def classify_event(
    event: EventInfo,
    series: SeriesInfo | None,
    *,
    asserted_exhaustive: frozenset[str] = frozenset(),
) -> Classification:
    """Decide whether ``event`` has structure the scanner can exploit, and build it.

    Run ``split_by_participant`` first: an event listing several participants' ladders is
    excluded here, but each participant's group on its own may be scannable.

    ``asserted_exhaustive`` holds event or series tickers whose categorical outcomes the user
    vouches are complete. It only ever *adds* an ASSERTED tier; LOGICAL is unaffected.
    """

    def excluded(reason: Exclusion, detail: str = "") -> Classification:
        return Classification(event, None, reason, detail)

    markets = event.markets
    if any(market.is_mve for market in markets):
        return excluded(Exclusion.MULTIVARIATE)
    if len(markets) < 2:
        return excluded(Exclusion.SINGLE_MARKET)
    non_binary = [market.ticker for market in markets if not market.is_binary]
    if non_binary:
        return excluded(Exclusion.NON_BINARY, ", ".join(non_binary[:3]))
    resolved = [market.ticker for market in markets if market.result == "yes"]
    if resolved:
        return excluded(Exclusion.RESOLVED, resolved[0])

    spaces: dict[Tier, OutcomeSpace] = {}
    intervals: dict[str, Interval] = {}
    strike_types = {market.strike_type for market in markets}
    if _all_interval_typed(markets):
        problem = _not_one_underlying(markets)
        if problem is not None:
            return excluded(*problem)
        try:
            intervals = {
                market.ticker: yes_interval(
                    market.strike_type, market.floor_strike, market.cap_strike
                )
                for market in markets
            }
            logical = interval_space(intervals)
        except StructureError as exc:
            return excluded(Exclusion.INVALID_STRIKES, str(exc))
        duplicate = _duplicate_interval(intervals)
        if duplicate is not None:
            return excluded(Exclusion.DUPLICATE_STRIKES, duplicate)
        touching = _touching_boundary(logical)
        if touching is not None:
            return excluded(Exclusion.AMBIGUOUS_BOUNDARIES, touching)
        overlaps = logical.overlaps()
        if event.mutually_exclusive and overlaps:
            return excluded(Exclusion.CONTRADICTORY, f"atom {logical.atoms[overlaps[0]].label}")
        kind = StructureKind.INTERVAL
        spaces[Tier.LOGICAL] = logical
        structural = structural_space(logical)
        if structural is not None:
            spaces[Tier.STRUCTURAL] = structural
    elif event.mutually_exclusive:
        kind = StructureKind.CATEGORICAL
        tickers = list(event.tickers)
        spaces[Tier.LOGICAL] = categorical_space(tickers, exhaustive=False)
        if {event.event_ticker, event.series_ticker} & asserted_exhaustive:
            spaces[Tier.ASSERTED] = categorical_space(tickers, exhaustive=True)
    elif strike_types & _UNSUPPORTED_TYPE_NAMES:
        return excluded(Exclusion.UNSUPPORTED_STRIKES)
    else:
        return excluded(Exclusion.NO_STRUCTURE)

    fees = resolve_fee_schedule(event, series)
    if fees.schedule is None:
        return excluded(Exclusion.FEES, fees.reason)
    return Classification(
        event, EventStructure(event, fees.schedule, kind, spaces, intervals), exclusion=None
    )


def _not_one_underlying(markets: Sequence[MarketInfo]) -> tuple[Exclusion, str] | None:
    """Evidence that strike markets do not all settle off one value at one time.

    * Different ``custom_strike`` participants: "Jalen Coker 40+ receiving yards" and
      "DJ Moore 40+ receiving yards" share a strike, not an underlying.
    * Different expiration times: "100M subscribers before 2027" and "... before 2028" reuse one
      strike for different deadlines, and "emissions at most X by 2025" and "at most Y by 2030"
      measure different years. A single settlement value settles once.
    """
    participants = {market.participant for market in markets}
    if len(participants) > 1:
        return Exclusion.MIXED_PARTICIPANTS, f"{len(participants)} participants"
    expirations = {market.latest_expiration_time for market in markets}
    if len(expirations) > 1:
        return Exclusion.STAGGERED_SETTLEMENT, f"{len(expirations)} distinct expiration times"
    return None


def _duplicate_interval(intervals: Mapping[str, Interval]) -> str | None:
    """Two markets with the same YES-interval can only differ in something strikes don't show."""
    seen: dict[Interval, str] = {}
    for ticker, interval in intervals.items():
        if interval in seen:
            return f"{seen[interval]} and {ticker} are both {interval}"
        seen[interval] = ticker
    return None


def _touching_boundary(space: OutcomeSpace) -> str | None:
    """Two markets whose YES-sets meet at exactly one shared endpoint.

    Brackets written [0, 2] and [2, 4] follow a half-open convention the API does not state, so
    which bracket owns 2 cannot be known. Kalshi's documented reading -- floor and cap both
    inclusive -- would pay both, and live mutually exclusive events prove it does not.
    """
    runs = [(ticker, run) for ticker in space.tickers if (run := space.run(ticker)) is not None]
    for index, (left, (left_first, left_last)) in enumerate(runs):
        for right, (right_first, right_last) in runs[index + 1 :]:
            shared = max(left_first, right_first)
            if (
                shared == min(left_last, right_last)
                and (left_last == right_first or right_last == left_first)
                and space.atoms[shared].kind is AtomKind.POINT
            ):
                return f"{left} and {right} both include {space.atoms[shared].label}"
    return None


def split_by_participant(event: EventInfo) -> tuple[EventInfo, ...]:
    """One sub-event per participant, for strike events that list several side by side.

    Kalshi's sports events put every player's (or team's) ladder in one event and identify each
    market's participant in ``custom_strike``. Markets about one participant can form a genuine
    ladder, so each group becomes its own sub-event, keyed ``EVENT#participant``. Everything
    else is returned unchanged -- including categorical events, whose outcomes legitimately
    name different participants.
    """
    groups: dict[tuple[tuple[str, str], ...], list[MarketInfo]] = defaultdict(list)
    for market in event.markets:
        groups[market.participant].append(market)
    if len(groups) < 2 or not _all_interval_typed(event.markets):
        return (event,)
    return tuple(
        replace(
            event,
            event_ticker=f"{event.event_ticker}#{_participant_label(participant)}",
            markets=tuple(members),
        )
        for participant, members in groups.items()
    )


def _participant_label(participant: tuple[tuple[str, str], ...]) -> str:
    if not participant:
        return "unattributed"
    return "+".join(f"{name}:{value[:8]}" for name, value in participant)


@dataclass(frozen=True, slots=True)
class TradeabilityRules:
    min_time_to_close: timedelta = timedelta(minutes=5)
    """Markets closing sooner than this are left alone: a basket must be completable."""


DEFAULT_TRADEABILITY: Final = TradeabilityRules()


def tradeable_tickers(
    event: EventInfo,
    *,
    now: datetime,
    trading_shards: frozenset[int] | None = None,
    rules: TradeabilityRules = DEFAULT_TRADEABILITY,
) -> frozenset[str]:
    """Markets a taker could trade right now.

    Untradeable markets still shape the outcome space -- they can still win -- they simply
    cannot be bought.
    """
    cutoff = now + rules.min_time_to_close
    return frozenset(
        market.ticker
        for market in event.markets
        if market.is_active
        and market.is_binary
        and (market.close_time is None or market.close_time > cutoff)
        and (trading_shards is None or market.exchange_index in trading_shards)
    )
