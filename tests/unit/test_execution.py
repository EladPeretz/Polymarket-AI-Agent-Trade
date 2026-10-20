from datetime import timedelta
from decimal import Decimal

import pytest

from perps_agent.execution import ExecutionError, SimBackend
from perps_agent.ledger import (
    FillBooked,
    FundingBooked,
    JsonlLedgerSink,
    Ledger,
    OrderPurpose,
    ProtectionChanged,
    load_events,
)
from perps_agent.risk import Decision, Direction, RiskCheck
from perps_agent.venue import OrderStatus, RequestRejected, Side, TimeInForce
from tests.unit.execution_factories import Desk
from tests.unit.risk_factories import NOW, intent

D = Decimal
SYMBOL = "NVDA-USD"


def _filled_long(desk: Desk, **overrides) -> None:
    desk.market(0)
    desk.enter(intent(entry=None, stop="199", **overrides))
    desk.market(300)  # past the 250 ms latency


def _assert_books_agree(desk: Desk) -> None:
    assert desk.run(desk.execution.mismatches()) == []
    assert desk.ledger.collateral == desk.sim.account.collateral


# --- Entries and protection ---


def test_marketable_entry_fills_and_gets_its_stop_at_once():
    desk = Desk()
    _filled_long(desk)
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None
    assert position.direction is Direction.LONG
    assert position.quantity == D("3.4313")  # 3.5 pUSD over 1.02
    assert position.average_entry == D("200.02")
    assert position.stop_price == D(199)
    assert position.approved_risk == D("3.5")
    assert desk.sim.protection(19).stop_loss == D(199)
    assert desk.sim.position(19) is not None and desk.sim.position(19).leverage == D(10)
    _assert_books_agree(desk)


def test_entry_order_is_recorded_with_its_approval():
    desk = Desk()
    desk.market(0)
    verdict = desk.enter(intent(entry=None, stop="199", target_price=D(205)))
    assert verdict.order is not None
    order = desk.ledger.order(verdict.order.client_order_id)
    assert order is not None
    sent = order.sent
    assert sent.purpose is OrderPurpose.ENTRY
    assert sent.time_in_force is TimeInForce.IOC
    assert (sent.stop_price, sent.target_price) == (D(199), D(205))
    assert sent.playbook == "orb@1"
    desk.market(300)
    assert desk.sim.protection(19).take_profit == D(205)
    assert desk.ledger.order(verdict.order.client_order_id).status is OrderStatus.FILLED


def test_short_entry():
    desk = Desk()
    desk.market(0)
    desk.enter(intent(Direction.SHORT, entry=None, stop="201"))
    desk.market(300)
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None and position.direction is Direction.SHORT
    assert desk.sim.position(19).quantity == -position.quantity
    assert desk.sim.protection(19).stop_loss == D(201)
    _assert_books_agree(desk)


def test_limit_entry_fills_on_a_trade_through_and_is_protected():
    desk = Desk()
    desk.market(0)
    desk.enter(intent(entry="199.90", stop="198.90"))
    desk.market(300)
    assert desk.ledger.open_position(SYMBOL) is None
    state = desk.run(desk.execution.risk_state())
    assert state.pending_entries == frozenset({SYMBOL})
    desk.trade(1_000, "199.80")
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None and position.stop_price == D("198.90")
    assert desk.sim.protection(19).stop_loss == D("198.90")
    _assert_books_agree(desk)


def test_limit_entry_is_cancelled_after_the_timeout():
    desk = Desk(entry_timeout_seconds=60)
    desk.market(0)
    verdict = desk.enter(intent(entry="199.50", stop="198.50"))
    desk.market(30_000)
    desk.tick()
    assert verdict.order is not None
    assert desk.ledger.order(verdict.order.client_order_id).is_live
    desk.market(60_000)
    desk.tick()
    assert desk.ledger.order(verdict.order.client_order_id).status is OrderStatus.CANCELLED
    assert desk.run(desk.execution.risk_state()).pending_entries == frozenset()


def test_entries_count_toward_the_day():
    desk = Desk()
    desk.market(0)
    desk.enter(intent(entry="199.50", stop="198.50"))
    state = desk.run(desk.execution.risk_state())
    assert state.trades_today == 1
    assert state.l2_trades_today == 0
    desk.run(desk.execution.start_day(NOW.date()))
    assert desk.run(desk.execution.risk_state()).trades_today == 0


def test_execution_refuses_what_risk_did_not_approve():
    desk = Desk()
    desk.market(0)
    rejected = desk.evaluate(intent(stop="201"))
    assert rejected.decision is Decision.REJECTED
    with pytest.raises(ExecutionError, match="rejected"):
        desk.run(desk.execution.enter(rejected))


def test_second_entry_on_the_same_symbol_is_refused():
    desk = Desk()
    desk.market(0)
    first = desk.evaluate(intent(entry="199.50", stop="198.50"))
    second = desk.evaluate(intent(entry="199.40", stop="198.40"))
    desk.run(desk.execution.enter(first))
    with pytest.raises(ExecutionError, match="already has"):
        desk.run(desk.execution.enter(second))


def test_a_venue_refusal_is_recorded_and_raised():
    desk = Desk(collateral="500")
    desk.market(0)
    verdict = desk.evaluate(intent(entry=None, stop="199"))
    desk.sim.account.collateral = D(1)  # margin vanished between risk and send
    with pytest.raises(RequestRejected):
        desk.run(desk.execution.enter(verdict))
    assert verdict.order is not None
    order = desk.ledger.order(verdict.order.client_order_id)
    assert order is not None and order.failed and not order.is_live


class _NoProtection(SimBackend):
    async def set_protection(self, instrument_id, stop_loss, take_profit):
        raise RequestRejected("tp/sl unavailable", status=400)


def test_a_position_that_cannot_be_protected_is_closed():
    desk = Desk()
    desk.execution._backend = _NoProtection(desk.sim)
    _filled_long(desk)
    assert SYMBOL in desk.execution.closing
    desk.market(600)
    assert desk.ledger.open_position(SYMBOL) is None
    assert desk.execution.closing == {}
    _assert_books_agree(desk)


# --- Stops, targets and exits ---


def test_stop_trigger_closes_the_position_and_is_booked():
    desk = Desk()
    _filled_long(desk)
    desk.market(1_000, mark="198.90", bid="198.88", ask="198.92")
    position = desk.ledger.positions[0]
    assert position.stop_price is None  # the venue cleared it when it fired
    desk.market(1_300)
    assert not position.is_open
    purposes = [
        desk.ledger.order(e.client_order_id).sent.purpose
        for e in desk.ledger.events
        if isinstance(e, FillBooked)
    ]
    assert purposes == [OrderPurpose.ENTRY, OrderPurpose.PROTECTION]
    assert position.realized_pnl == desk.sim.account.realized_pnl
    assert position.fees == desk.sim.account.fees_paid
    assert position.net_pnl < 0
    _assert_books_agree(desk)


def test_close_position_sends_a_limited_reduce_only_ioc():
    desk = Desk()
    _filled_long(desk)
    state = desk.run(desk.execution.close_position(SYMBOL, "thesis invalidated"))
    assert state is not None
    request = state.request
    assert request.reduce_only and request.time_in_force is TimeInForce.IOC
    assert request.side is Side.SELL
    assert request.price == D(199)  # 200 × (1 − 50 bp)
    desk.drain()
    desk.market(600)
    position = desk.ledger.positions[0]
    assert not position.is_open
    assert desk.ledger.order(request.client_order_id).sent.reason == "thesis invalidated"
    _assert_books_agree(desk)


def test_a_partly_filled_close_is_resent_until_flat():
    desk = Desk(collateral="2000")
    _filled_long(desk)  # 14 pUSD of risk: about 13.7 units
    desk.run(desk.execution.close_position(SYMBOL, "window close"))
    desk.drain()
    desk.market(600, quantity="5")  # only 5 at the bid, the next level is past the limit
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None and position.quantity < D("13.7")
    desk.tick()
    desk.market(900)
    assert desk.ledger.open_position(SYMBOL) is None
    assert desk.execution.closing == {}
    _assert_books_agree(desk)


def test_reduce_position_takes_part_off():
    desk = Desk()
    _filled_long(desk)
    desk.run(desk.execution.reduce_position(SYMBOL, D("0.5"), "first target"))
    desk.drain()
    desk.market(600)
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None
    assert position.quantity == D("3.4313") - D("1.7156")
    assert position.realized_pnl < 0  # sold at the bid
    _assert_books_agree(desk)


def test_reduce_everything_is_a_close():
    desk = Desk()
    _filled_long(desk)
    state = desk.run(desk.execution.reduce_position(SYMBOL, D(1), "done"))
    order = desk.ledger.order(state.request.client_order_id)
    assert order.sent.purpose is OrderPurpose.EXIT
    with pytest.raises(ExecutionError):
        desk.run(desk.execution.reduce_position(SYMBOL, D(0), "nothing"))


def test_stops_only_tighten_through_execution():
    desk = Desk()
    _filled_long(desk)
    tighter = desk.run(
        desk.execution.update_protection(SYMBOL, stop_price=D("199.5"), reason="trail")
    )
    assert tighter is None
    assert desk.sim.protection(19).stop_loss == D("199.5")
    looser = desk.run(desk.execution.update_protection(SYMBOL, stop_price=D(199), reason="oops"))
    assert looser is not None and looser.check is RiskCheck.STOP_WIDENED
    assert desk.sim.protection(19).stop_loss == D("199.5")
    assert desk.ledger.open_position(SYMBOL).stop_price == D("199.5")
    changes = [e for e in desk.ledger.events if isinstance(e, ProtectionChanged)]
    assert [c.reason for c in changes] == ["entry filled", "trail"]


def test_targets_can_move_or_be_removed():
    desk = Desk()
    _filled_long(desk, target_price=D(205))
    moved = desk.run(desk.execution.update_protection(SYMBOL, target_price=D(210), reason="run"))
    assert moved is None and desk.sim.protection(19).take_profit == D(210)
    wrong = desk.run(desk.execution.update_protection(SYMBOL, target_price=D(195), reason="x"))
    assert wrong is not None and wrong.check is RiskCheck.TARGET_SIDE
    desk.run(desk.execution.update_protection(SYMBOL, remove_target=True, reason="runner"))
    protection = desk.sim.protection(19)
    assert protection.take_profit is None and protection.stop_loss == D(199)


def test_time_stop_closes_the_position():
    desk = Desk()
    _filled_long(desk, time_stop=NOW + timedelta(minutes=5))
    desk.tick()
    assert desk.ledger.open_position(SYMBOL) is not None
    desk.market(5 * 60_000)
    desk.tick()
    desk.market(5 * 60_000 + 300)
    assert desk.ledger.open_position(SYMBOL) is None


def test_close_all_cancels_entries_and_closes_positions():
    desk = Desk()
    _filled_long(desk)
    desk.run(desk.execution.close_all("guardian: window close"))
    desk.drain()
    desk.market(600)
    assert desk.ledger.open_positions == ()
    assert desk.ledger.live_orders() == ()


# --- Adding ---


def test_add_to_a_winner_tightens_the_stop_and_stays_in_budget():
    desk = Desk()
    _filled_long(desk)
    desk.market(10_000, mark="202", bid="201.98", ask="202.02")
    # Moving the stop to 200.5 puts it above the 200.02 entry: the first entry is safe.
    verdict = desk.enter(intent(entry=None, stop="200.5"), adding=True)
    assert verdict.adding
    desk.market(10_300)
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None
    assert position.quantity > D("3.4313")
    assert position.stop_price == D("200.5")
    assert desk.sim.protection(19).stop_loss == D("200.5")
    assert position.risk(D(202)).risk_to_stop <= position.approved_risk
    _assert_books_agree(desk)


# --- Funding and the event log ---


def test_funding_is_booked_to_the_position():
    desk = Desk()
    desk.next_funding = NOW + timedelta(seconds=2)
    _filled_long(desk)
    desk.market(3_000, rate="0.0001")
    funding = [e for e in desk.ledger.events if isinstance(e, FundingBooked)]
    assert len(funding) == 1
    assert funding[0].amount < 0  # longs pay a positive rate
    assert desk.ledger.open_position(SYMBOL).funding == funding[0].amount
    _assert_books_agree(desk)


def test_the_event_log_rebuilds_the_ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    sink = JsonlLedgerSink(path)
    desk = Desk()
    desk.ledger._sink = sink
    _filled_long(desk)
    desk.market(1_000, mark="198.90", bid="198.88", ask="198.92")
    desk.market(1_300)
    sink.close()
    rebuilt = Ledger.from_events(D(500), load_events(path))
    assert rebuilt.events == desk.ledger.events
    assert rebuilt.collateral == desk.ledger.collateral
    assert [p.net_pnl for p in rebuilt.positions] == [p.net_pnl for p in desk.ledger.positions]
    assert [e.sequence for e in rebuilt.events] == list(range(1, len(rebuilt.events) + 1))
