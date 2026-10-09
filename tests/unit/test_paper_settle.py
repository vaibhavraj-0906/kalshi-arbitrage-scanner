from __future__ import annotations

from datetime import UTC, datetime

from karb.core.fixed import Cash, Price, Qty
from karb.market.book import Side
from karb.paper.settle import MarketResult, holdings, market_result, settlement_payout
from karb.store.database import PaperOrderRow
from karb.wire.models import MarketWire

SETTLED = datetime(2026, 10, 1, tzinfo=UTC)


def result(ticker: str, value: str | None, status: str = "finalized") -> MarketResult:
    return MarketResult(ticker, status, "", None if value is None else Price.parse(value), SETTLED)


def wire(**fields: object) -> MarketWire:
    return MarketWire.model_validate({"ticker": "T", "event_ticker": "E", **fields})


def test_market_results_from_the_wire() -> None:
    yes = market_result(wire(status="finalized", result="yes"))
    assert (yes.yes_value, yes.final) == (Price.ONE, True)
    no = market_result(wire(status="finalized", result="no", settlement_value_dollars="0.0000"))
    assert (no.yes_value, no.final) == (Price.ZERO, True)
    # A settlement value wins over the result: it also covers voided and scalar markets.
    partial = market_result(
        wire(status="finalized", result="scalar", settlement_value_dollars="0.3700")
    )
    assert partial.yes_value == Price.parse("0.37")
    # Announced but not final: disputes and amendments are still possible.
    assert not market_result(wire(status="determined", result="yes")).final
    assert not market_result(wire(status="active", result="")).final


def test_payout_pays_yes_the_value_and_no_its_complement() -> None:
    held = [
        ("A", Side.YES, Qty.contracts(10)),
        ("A", Side.NO, Qty.contracts(4)),
        ("B", Side.NO, Qty.parse("2.50")),
    ]
    results = {"A": result("A", "1.0000"), "B": result("B", "0.3700")}
    # A: 10 YES pay $10, 4 NO pay $0. B: 2.5 NO pay 2.5 x 0.63 = $1.575.
    assert settlement_payout(held, results) == Cash.parse("11.575")


def test_payout_waits_for_every_market_to_be_final() -> None:
    held = [("A", Side.YES, Qty.contracts(1)), ("B", Side.NO, Qty.contracts(1))]
    assert settlement_payout(held, {"A": result("A", "1.0000")}) is None
    pending = {"A": result("A", "1.0000"), "B": result("B", "0.0000", status="determined")}
    assert settlement_payout(held, pending) is None


def test_holdings_sum_entry_and_hedge_fills_only() -> None:
    def order(phase: str, ticker: str, side: str, filled: int) -> PaperOrderRow:
        return PaperOrderRow("T1", phase, 0, ticker, side, 6000, filled, filled, 0, 0, "[]")

    rows = [
        order("plan", "A", "no", 5000),
        order("entry", "A", "no", 5000),
        order("entry", "C", "no", 0),
        order("hedge", "A", "yes", 5000),
        order("hedge", "A", "no", 100),
    ]
    assert holdings(rows) == [
        ("A", Side.NO, Qty(5100)),
        ("A", Side.YES, Qty(5000)),
    ]
