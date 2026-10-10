"""Exercising the trading path on demand: ``karb trade --exercise EVENT`` (ADR-0010).

Real arbitrage is rare -- on the demo exchange, whose markets need not resemble real ones, it may
never appear while someone is watching. The exercise shows the whole path anyway, on a basket
whose payout is known in advance: one **complete set**, a group of legs that pays out exactly once
in every outcome. karb prefers YES on markets that partition the outcomes (a ladder's brackets and
tails, say). When the books cannot supply that -- thin demo ladders rarely quote every bracket --
it falls back to YES and NO on the single market where the pair is cheapest. The exchange nets
that pair at once, returning $1 a set, which exercises the netting reconciliation too.

A complete set always pays exactly $1 per set. It usually costs a little more, so its guaranteed
P&L is a small known loss. Everything else is the real thing:
- signed batched orders;
- exact fills and fees, audited against the fee model;
- repair of a partial fill;
- positions and balance reconciled with the exchange;
- attribution;
- later, a settlement that must pay exactly what the model guaranteed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from karb.arb.detect import EventSnapshot, opportunity_from_basket
from karb.arb.opportunity import ArbKind, VerifiedBasket
from karb.arb.verify import verify_basket
from karb.core.fixed import Qty
from karb.exchange.client import KalshiClient
from karb.exchange.endpoints import (
    fetch_event,
    fetch_exchange_status,
    fetch_one_series,
    fetch_orderbooks,
    pack_batches,
    trading_shards,
)
from karb.market.book import OrderBook, Side
from karb.market.fees import FeeConfig
from karb.structure.classify import Tier, classify_event, split_by_participant, tradeable_tickers
from karb.structure.intervals import OutcomeSpace
from karb.trading.orders import plan_orders
from karb.trading.plan import TradePlan

__all__ = ["complete_set", "exact_cover", "fetch_event_snapshots"]


async def fetch_event_snapshots(
    client: KalshiClient, event_ticker: str, *, max_levels: int
) -> tuple[list[EventSnapshot], list[str]]:
    """A snapshot of each scannable group in the event, and why the others are not."""
    shards = trading_shards(await fetch_exchange_status(client))
    event = await fetch_event(client, event_ticker)
    series = await fetch_one_series(client, event.series_ticker)
    books: dict[str, OrderBook] = {}
    sent = received = client.clock.monotonic()
    for batch in pack_batches([sorted(event.tickers)]):
        fetched = await fetch_orderbooks(client, batch, depth=max_levels)
        books.update(fetched.books)
        received = fetched.received_at
    now = client.clock.now()
    snapshots: list[EventSnapshot] = []
    excluded: list[str] = []
    for group in split_by_participant(event):
        classification = classify_event(group, series)
        if classification.structure is None:
            reason = classification.exclusion.value if classification.exclusion else "unknown"
            excluded.append(f"{group.event_ticker}: {reason}")
            continue
        tradeable = tradeable_tickers(group, now=now, trading_shards=shards)
        snapshots.append(
            EventSnapshot(classification.structure, books, tradeable, now, received - sent)
        )
    return snapshots, excluded


def exact_cover(space: OutcomeSpace, candidates: Iterable[str]) -> tuple[str, ...] | None:
    """Markets whose YES-sets partition the outcome space, if any do."""
    atoms_of = {t: space.yes_atoms[t] for t in sorted(candidates) if space.yes_atoms.get(t)}
    everything = frozenset(range(space.size))

    def search(covered: frozenset[int], chosen: tuple[str, ...]) -> tuple[str, ...] | None:
        if covered == everything:
            return chosen
        target = min(everything - covered)
        for ticker, atoms in atoms_of.items():
            if target in atoms and not atoms & covered:
                found = search(covered | atoms, (*chosen, ticker))
                if found is not None:
                    return found
        return None

    return search(frozenset(), ())


def _short(tickers: list[str]) -> str:
    shown = ", ".join(tickers[:3])
    return shown if len(tickers) <= 3 else f"{shown} and {len(tickers) - 3} more"


def complete_set(snapshot: EventSnapshot, *, sets: int, fee_config: FeeConfig) -> TradePlan | str:
    """A plan buying ``sets`` complete sets, or the reason there is none."""
    if sets < 1:
        return "buy at least one set"
    structure = snapshot.structure
    usable = {
        t for t in snapshot.tradeable if t in snapshot.books and not snapshot.books[t].is_crossed
    }
    want = Qty.contracts(sets)
    reasons: list[str] = []
    for tier, space in structure.spaces.items():
        cover = exact_cover(space, usable)
        if cover is None:
            continue
        quantities = {(ticker, Side.YES): want for ticker in cover}
        basket = verify_basket(space, snapshot.books, quantities, structure.fees, fee_config)
        filled = {leg.ticker: leg.order.qty for leg in basket.legs}
        short = sorted(t for t in cover if filled.get(t, Qty.ZERO) < want)
        if not short:
            return _plan(snapshot, tier, space, basket)
        reasons.append(f"too little YES on {_short(short)} for a set of brackets")
        break
    else:
        reasons.append("no markets partition the outcomes")

    # Fallback: YES and NO on one market is also a complete set.
    pairs: dict[str, int] = {}
    for ticker in usable:
        book = snapshot.books[ticker]
        yes, no = book.best_ask(Side.YES), book.best_ask(Side.NO)
        if yes is not None and no is not None and yes.qty >= want and no.qty >= want:
            pairs[ticker] = yes.price.raw + no.price.raw
    if pairs:
        ticker = min(sorted(pairs), key=pairs.__getitem__)
        tier, space = next(iter(structure.spaces.items()))
        quantities = {(ticker, Side.YES): want, (ticker, Side.NO): want}
        basket = verify_basket(space, snapshot.books, quantities, structure.fees, fee_config)
        return _plan(snapshot, tier, space, basket)
    reasons.append(f"no market quotes both YES and NO for {sets} contract(s)")
    return "no complete set can be bought: " + "; ".join(reasons)


def _plan(
    snapshot: EventSnapshot,
    tier: Tier,
    space: OutcomeSpace,
    basket: VerifiedBasket,
) -> TradePlan:
    structure = snapshot.structure
    opportunity = replace(
        opportunity_from_basket(snapshot, tier, space, basket), kind=ArbKind.EXERCISE
    )
    return TradePlan(
        opportunity, structure.event.event_ticker, structure.fees, basket, plan_orders(basket)
    )
