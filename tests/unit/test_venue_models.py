from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from perps_agent.venue.models import (
    BookLevel,
    InstrumentCategory,
    OrderBook,
    OrderStatus,
    Side,
    TimeInForce,
)
from tests.unit.venue_factories import T0, instrument, order


def test_leverage_cap_follows_risk_tiers() -> None:
    inst = instrument()
    assert inst.max_leverage_for(Decimal(10_000)) == 10
    assert inst.max_leverage_for(Decimal(500_000)) == 5
    assert inst.max_leverage_for(Decimal(2_000_000)) == 3


def test_instrument_derived_values() -> None:
    inst = instrument()
    assert inst.tick_size == Decimal("0.01")
    assert inst.lot_size == Decimal("0.0001")
    assert inst.maintenance_margin_rate == Decimal("0.05")
    assert inst.accepts_new_exposure
    assert not instrument(close_only=True).accepts_new_exposure


def test_unknown_enum_values_do_not_break_parsing() -> None:
    assert InstrumentCategory.parse("etf") is InstrumentCategory.UNKNOWN
    assert OrderStatus.parse("brand_new_status") is OrderStatus.UNKNOWN
    unknown = OrderStatus.UNKNOWN
    assert not unknown.is_live and not unknown.is_terminal
    assert OrderStatus.PARTIAL.is_live and OrderStatus.IOC_NO_FILL.is_terminal


def test_timestamps_are_normalised_to_utc_and_naive_rejected() -> None:
    plus2 = timezone(timedelta(hours=2))
    book = OrderBook(instrument_id=19, bids=(), asks=(), timestamp=T0.astimezone(plus2), sequence=1)
    assert book.timestamp == T0 and book.timestamp.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        OrderBook(instrument_id=19, bids=(), asks=(), timestamp=datetime(2026, 1, 1), sequence=1)


def test_book_sides_must_be_sorted() -> None:
    low, high = (
        BookLevel(price=Decimal(1), quantity=Decimal(1)),
        BookLevel(price=Decimal(2), quantity=Decimal(1)),
    )
    with pytest.raises(ValidationError):
        OrderBook(instrument_id=1, bids=(low, high), asks=(), timestamp=T0, sequence=1)
    with pytest.raises(ValidationError):
        OrderBook(instrument_id=1, bids=(), asks=(high, low), timestamp=T0, sequence=1)


def test_order_request_validation() -> None:
    with pytest.raises(ValidationError):
        order(coid="not-hex")
    with pytest.raises(ValidationError):
        order(price=None, time_in_force=TimeInForce.GTC)
    with pytest.raises(ValidationError):
        order(time_in_force=TimeInForce.IOC, post_only=True)
    with pytest.raises(ValidationError):
        order(quantity="0")
    assert order(price=None).is_market


def test_models_are_immutable() -> None:
    inst = instrument()
    with pytest.raises(ValidationError):
        inst.min_notional = Decimal(1)  # type: ignore[misc]


def test_side() -> None:
    assert Side.BUY.sign == 1 and Side.SELL.sign == -1
    assert Side.BUY.opposite is Side.SELL
