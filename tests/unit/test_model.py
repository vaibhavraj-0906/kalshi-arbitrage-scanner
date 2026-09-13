from __future__ import annotations

from decimal import Decimal

from karb.market.model import market_from_wire
from karb.wire.models import ExchangeStatusWire, MarketWire
from tests.support import fixture_books, fixture_event, load_fixture


def test_markets_page_converts() -> None:
    payload = load_fixture("markets_page.json")
    markets = [market_from_wire(MarketWire.model_validate(raw)) for raw in payload["markets"]]
    assert len(markets) == 50
    assert payload["cursor"]
    assert not any(m.is_mve for m in markets)


def test_strikes_decode_as_exact_decimals() -> None:
    ev = fixture_event("events_KXINX.json")
    strikes = [s for m in ev.markets for s in (m.floor_strike, m.cap_strike) if s is not None]
    assert strikes
    assert all(type(s) is Decimal for s in strikes)
    assert any(s.as_tuple().exponent == -4 for s in strikes)  # e.g. 7249.9999, held exactly


def test_every_fixture_event_has_a_book_per_market() -> None:
    for event_file, books_file in [
        ("events_KXINX.json", "orderbooks_KXINX.json"),
        ("events_KXBTCD.json", "orderbooks_KXBTCD.json"),
        ("event_KXNEXTDNCCHAIR-45.json", "orderbooks_KXNEXTDNCCHAIR-45.json"),
    ]:
        assert set(fixture_event(event_file).tickers) == set(fixture_books(books_file))


def test_exchange_status_parses() -> None:
    status = ExchangeStatusWire.model_validate(load_fixture("exchange_status.json"))
    assert 0 in {s.exchange_index for s in status.exchange_index_statuses}
