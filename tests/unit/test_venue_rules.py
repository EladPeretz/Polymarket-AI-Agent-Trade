from decimal import Decimal

import pytest

from perps_agent.venue.models import Side
from perps_agent.venue.rules import (
    Violation,
    check_order,
    decimal_places,
    round_price,
    round_quantity,
    significant_figures,
)
from tests.unit.venue_factories import instrument, order

MARK = Decimal(200)


def rules(request, inst=None, mark=MARK) -> set[Violation]:
    return {v.rule for v in check_order(inst or instrument(), request, mark)}


@pytest.mark.parametrize(
    ("value", "figures"),
    [("216.27", 5), ("21600", 3), ("0.0001", 1), ("100000", 1), ("123456", 6), ("0", 0)],
)
def test_significant_figures(value: str, figures: int) -> None:
    assert significant_figures(Decimal(value)) == figures


def test_decimal_places() -> None:
    assert decimal_places(Decimal("1.2300")) == 2
    assert decimal_places(Decimal("1E+3")) == 0


def test_round_price_is_passive_by_side() -> None:
    inst = instrument()
    assert round_price(inst, Decimal("216.275"), Side.BUY) == Decimal("216.27")
    assert round_price(inst, Decimal("216.271"), Side.SELL) == Decimal("216.28")
    assert round_price(inst, Decimal("216.275")) == Decimal("216.28")


def test_round_price_respects_five_significant_figures() -> None:
    btc = instrument(price_decimals=1)
    assert round_price(btc, Decimal("107012.5"), Side.BUY) == Decimal("107010")
    assert round_price(btc, Decimal("107012.5"), Side.SELL) == Decimal("107020")
    assert significant_figures(round_price(btc, Decimal("107012.5"))) <= 5


def test_round_quantity_never_rounds_up() -> None:
    inst = instrument()
    assert round_quantity(inst, Decimal("1.23456")) == Decimal("1.2345")
    assert round_quantity(inst, Decimal("123.456789")) == Decimal("123.45")
    assert round_quantity(inst, Decimal("0.00009")) == Decimal("0.0000")


def test_valid_order_passes() -> None:
    assert check_order(instrument(), order(quantity="0.5", price="200.01"), MARK) == ()


def test_precision_and_figures() -> None:
    assert rules(order(price="200.015")) == {
        Violation.PRICE_PRECISION,
        Violation.PRICE_SIGNIFICANT_FIGURES,
    }
    assert rules(order(quantity="0.12345")) == {Violation.QUANTITY_PRECISION}
    assert rules(order(quantity="12.3456", price="2")) == {
        Violation.QUANTITY_SIGNIFICANT_FIGURES,
        Violation.PRICE_OUTSIDE_BAND,
    }


def test_price_band() -> None:
    assert rules(order(price="180")) == set()
    assert rules(order(price="179.99")) == {Violation.PRICE_OUTSIDE_BAND}
    assert rules(order(price="220.01")) == {Violation.PRICE_OUTSIDE_BAND}


def test_min_notional_is_not_applied_to_exits() -> None:
    small = order(quantity="0.04", price="200")  # $8
    assert rules(small) == {Violation.BELOW_MIN_NOTIONAL}
    assert rules(order(quantity="0.04", price="200", reduce_only=True)) == set()


def test_max_notional_market_and_limit() -> None:
    inst = instrument(max_market_notional=Decimal(1000), max_limit_notional=Decimal(2000))
    # A market buy may fill up to the top of the band: 5 × 200 × 1.10 = 1100.
    assert rules(order(quantity="5", price=None), inst) == {Violation.ABOVE_MAX_NOTIONAL}
    assert rules(order(quantity="5", price=None, side=Side.SELL), inst) == set()
    assert rules(order(quantity="9", price="200"), inst) == set()
    assert rules(order(quantity="11", price="200"), inst) == {Violation.ABOVE_MAX_NOTIONAL}


def test_close_only_and_settled_instruments() -> None:
    close_only = instrument(close_only=True)
    assert rules(order(), close_only) == {Violation.INSTRUMENT_CLOSE_ONLY}
    assert rules(order(reduce_only=True), close_only) == set()
    assert Violation.INSTRUMENT_SETTLED in rules(order(reduce_only=True), instrument(settled=True))


def test_wrong_instrument() -> None:
    assert Violation.INSTRUMENT_MISMATCH in rules(order(instrument_id=1))
