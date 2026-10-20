from decimal import Decimal

import pytest

from perps_agent.ledger import (
    DayStarted,
    FillBooked,
    FundingBooked,
    Ledger,
    LedgerError,
    OrderChanged,
    OrderPurpose,
    OrderSent,
    ProtectionChanged,
)
from perps_agent.risk import AutonomyLevel, Direction
from perps_agent.venue import OrderStatus, Side, TimeInForce
from tests.unit.risk_factories import NOW

D = Decimal


def _sent(coid: str, purpose: OrderPurpose, side: Side, **fields) -> OrderSent:
    return OrderSent(
        at=NOW,
        client_order_id=coid,
        symbol="NVDA-USD",
        instrument_id=19,
        position_id="p1",
        purpose=purpose,
        side=side,
        quantity=D(2),
        price=D(200),
        time_in_force=TimeInForce.GTC,
        **fields,
    )


def _fill(coid: str, side: Side, price: str, quantity: str, fee: str = "0.1") -> FillBooked:
    return FillBooked(
        at=NOW,
        client_order_id=coid,
        position_id="p1",
        symbol="NVDA-USD",
        trade_id=1,
        side=side,
        price=D(price),
        quantity=D(quantity),
        fee=D(fee),
        is_taker=True,
    )


def _long(ledger: Ledger) -> None:
    ledger.record(
        _sent(
            "a" * 32,
            OrderPurpose.ENTRY,
            Side.BUY,
            level=AutonomyLevel.L2,
            stop_price=D(198),
            approved_risk=D(4),
        )
    )
    ledger.record(_fill("a" * 32, Side.BUY, "200", "2"))


def test_positions_and_pnl_are_derived_from_fills():
    ledger = Ledger(D(1000))
    _long(ledger)
    ledger.record(_sent("b" * 32, OrderPurpose.ADD, Side.BUY, stop_price=D(199)))
    ledger.record(_fill("b" * 32, Side.BUY, "203", "1"))
    position = ledger.open_position("NVDA-USD")
    assert position is not None
    assert position.direction is Direction.LONG
    assert position.quantity == D(3)
    assert position.average_entry == D(201)
    assert position.planned_stop == D(199)  # the add tightened it
    assert position.approved_risk == D(4)
    assert ledger.entries_today == 2 and ledger.l2_entries_today == 1

    ledger.record(_sent("c" * 32, OrderPurpose.EXIT, Side.SELL, reduce_only=True))
    ledger.record(_fill("c" * 32, Side.SELL, "205", "3"))
    assert position.realized_pnl == D(12)
    assert position.fees == D("0.3")
    assert not position.is_open and position.max_quantity == D(3)
    assert ledger.open_positions == ()
    assert ledger.collateral == D("1011.7")
    assert ledger.equity({}) == D("1011.7")


def test_equity_marks_open_positions():
    ledger = Ledger(D(1000))
    _long(ledger)
    assert ledger.equity({"NVDA-USD": D(201)}) == D("1001.9")
    assert ledger.equity({}) == D("999.9")  # no mark: valued at entry


def test_funding_and_protection_apply_to_the_position():
    ledger = Ledger(D(1000))
    _long(ledger)
    ledger.record(
        FundingBooked(
            at=NOW,
            position_id="p1",
            symbol="NVDA-USD",
            rate=D("0.0001"),
            mark_price=D(200),
            amount=D("-0.04"),
        )
    )
    ledger.record(
        ProtectionChanged(
            at=NOW,
            position_id="p1",
            symbol="NVDA-USD",
            stop_price=D(198),
            target_price=None,
            reason="entry filled",
        )
    )
    position = ledger.open_position("NVDA-USD")
    assert position is not None
    assert position.funding == D("-0.04") and position.stop_price == D(198)
    assert ledger.collateral == D("999.86")


def test_order_status_and_day_counters():
    ledger = Ledger(D(1000))
    ledger.record(_sent("a" * 32, OrderPurpose.ENTRY, Side.BUY))
    assert len(ledger.live_orders("NVDA-USD", (OrderPurpose.ENTRY,))) == 1
    ledger.record(
        OrderChanged(
            at=NOW,
            client_order_id="a" * 32,
            status=OrderStatus.CANCELLED,
            filled_quantity=D(0),
            resting_quantity=D(0),
        )
    )
    assert ledger.live_orders() == ()
    ledger.record(DayStarted(at=NOW, day=NOW.date(), equity=D(990)))
    assert ledger.entries_today == 0 and ledger.day_start_equity == D(990)


def test_contradictions_raise():
    ledger = Ledger(D(1000))
    with pytest.raises(LedgerError, match="unknown order"):
        ledger.record(_fill("a" * 32, Side.BUY, "200", "1"))
    ledger.record(_sent("e" * 32, OrderPurpose.EXIT, Side.SELL))
    with pytest.raises(LedgerError, match="unknown position"):
        ledger.record(_fill("e" * 32, Side.SELL, "200", "1"))
    _long(ledger)
    with pytest.raises(LedgerError, match="twice"):
        ledger.record(_sent("a" * 32, OrderPurpose.ENTRY, Side.BUY))
    ledger.record(_sent("c" * 32, OrderPurpose.EXIT, Side.SELL))
    with pytest.raises(LedgerError, match="flip"):
        ledger.record(_fill("c" * 32, Side.SELL, "200", "3"))


def test_sequence_numbers_and_replay():
    ledger = Ledger(D(1000))
    _long(ledger)
    assert [e.sequence for e in ledger.events] == [1, 2]
    rebuilt = Ledger.from_events(D(1000), ledger.events)
    assert rebuilt.open_position("NVDA-USD") == ledger.open_position("NVDA-USD")
