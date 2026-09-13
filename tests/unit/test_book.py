from __future__ import annotations

import pytest

from karb.core.fixed import Price, Qty
from karb.market.book import BookIntegrityError, OrderBook, Side
from tests.support import book, fixture_books, lv


def test_wire_ladders_are_reversed_to_best_first() -> None:
    # Shape of the live KXNEXTDNCCHAIR-45-MOMA book: NO bids only, ascending on the wire.
    ob = OrderBook.from_wire(
        "MOMA",
        yes_dollars=[],
        no_dollars=[("0.1300", "1115.01"), ("0.3200", "830.00"), ("0.8200", "200.00")],
    )
    assert ob.best_bid(Side.NO) == lv("0.8200", "200.00")
    assert ob.best_bid(Side.YES) is None
    # Buying YES takes the best NO bid: 1 - 0.82 = 0.18, matching the listing's yes_ask.
    assert ob.best_ask(Side.YES) == lv("0.1800", "200.00")
    assert [level.price for level in ob.asks(Side.YES)] == [
        Price.parse("0.1800"),
        Price.parse("0.6800"),
        Price.parse("0.8700"),
    ]
    assert ob.asks(Side.NO) == ()


def test_sweep_walks_levels_cheapest_first() -> None:
    ob = book("T", no=[("0.60", "5"), ("0.55", "10")])
    fills, remaining = ob.sweep(Side.YES, Qty.contracts(8))
    assert fills == (lv("0.40", "5"), lv("0.45", "3"))
    assert remaining.is_zero
    fills, remaining = ob.sweep(Side.YES, Qty.contracts(20))
    assert sum(f.qty.raw for f in fills) == Qty.contracts(15).raw
    assert remaining == Qty.contracts(5)


def test_crossed_book_is_detected() -> None:
    assert book("T", yes=[("0.40", "1")], no=[("0.60", "1")]).is_crossed
    assert not book("T", yes=[("0.40", "1")], no=[("0.59", "1")]).is_crossed


@pytest.mark.parametrize(
    "no_dollars",
    [
        [("0.3000", "1.00"), ("0.2000", "1.00")],  # descending on the wire
        [("0.3000", "1.00"), ("0.3000", "2.00")],  # duplicate price
        [("0.3000", "0.00")],  # empty level
        [("0.30001", "1.00")],  # inexact price
    ],
)
def test_malformed_books_are_integrity_errors(no_dollars: list[tuple[str, str]]) -> None:
    with pytest.raises(BookIntegrityError):
        OrderBook.from_wire("T", [], no_dollars)


def test_every_recorded_book_is_valid_and_uncrossed() -> None:
    for name in (
        "orderbooks_KXINX.json",
        "orderbooks_KXBTCD.json",
        "orderbooks_KXNEXTDNCCHAIR-45.json",
    ):
        books = fixture_books(name)
        assert books
        for ob in books.values():
            assert not ob.is_crossed, ob.ticker
