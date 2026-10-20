import asyncio
import logging
from datetime import timedelta
from decimal import Decimal

import pytest

from perps_agent.sim import (
    FillEvent,
    FundingPayment,
    Liquidation,
    OrderUpdate,
    PaperSimulator,
    PositionProtection,
    ProtectionTriggered,
)
from perps_agent.venue import OrderGateway, OrderStatus, RequestRejected, Side, TimeInForce
from perps_agent.venue.interfaces import AccountReader
from tests.unit.sim_factories import (
    at,
    book_event,
    simulator,
    ticker_event,
    trades_event,
)
from tests.unit.venue_factories import T0, order

D = Decimal


def _ready() -> PaperSimulator:
    sim, _ = simulator()
    sim.on_event(ticker_event(0))
    sim.on_event(book_event(0))
    return sim


def _fills(events: list) -> list:
    return [e.fill for e in events if isinstance(e, FillEvent)]


def _coid(n: int) -> str:
    return f"{n:032x}"


def test_satisfies_the_venue_interfaces():
    sim = _ready()
    gateway: OrderGateway = sim
    reader: AccountReader = sim
    assert gateway is reader


# --- Marketable orders and latency ---


def test_market_order_fills_against_the_first_book_after_the_latency():
    sim = _ready()
    state = sim.submit(order("2", price=None))
    assert state.status is OrderStatus.ACCEPTED
    # The book at 100 ms is before arrival (250 ms): nothing happens.
    assert _fills(sim.on_event(book_event(100))) == []
    # The book moved up by the time the order arrives; it fills there.
    events = sim.on_event(book_event(300, asks=(("200.5", "1"), ("200.6", "10"))))
    fills = _fills(events)
    assert [(f.price, f.quantity) for f in fills] == [(D("200.5"), D(1)), (D("200.6"), D(1))]
    assert all(f.is_taker for f in fills)
    assert fills[0].fee == D("200.5") * D("0.0004")
    assert sim.order(_coid(0)).status is OrderStatus.FILLED
    assert sim.position(19).quantity == 2


def test_ioc_without_enough_depth_expires_the_rest():
    sim = _ready()
    sim.submit(order("8", price="200.1", time_in_force=TimeInForce.IOC))
    fills = _fills(sim.on_event(book_event(300)))
    assert sum(f.quantity for f in fills) == 5
    state = sim.order(_coid(0))
    assert (state.status, state.filled_quantity) == (OrderStatus.IOC_EXPIRED, 5)


def test_ioc_with_nothing_inside_its_limit_does_not_fill():
    sim = _ready()
    sim.submit(order("1", price="200", time_in_force=TimeInForce.IOC))
    sim.on_event(book_event(300))
    assert sim.order(_coid(0)).status is OrderStatus.IOC_NO_FILL


def test_fok_fills_in_full_or_not_at_all():
    sim = _ready()
    sim.submit(order("20", price="200.2", time_in_force=TimeInForce.FOK))
    sim.submit(order("6", price="200.2", time_in_force=TimeInForce.FOK, client_order_id=_coid(1)))
    sim.on_event(book_event(300))
    assert sim.order(_coid(0)).status is OrderStatus.FOK_UNFILLED
    assert sim.order(_coid(1)).status is OrderStatus.FILLED
    assert sim.position(19).quantity == 6


def test_liquidity_taken_stays_taken_until_the_next_book():
    sim = _ready()
    sim.submit(order("4", price=None))
    sim.submit(order("4", price=None, client_order_id=_coid(1)))
    fills = _fills(sim.on_event(book_event(300)))
    assert [(f.price, f.quantity) for f in fills] == [
        (D("200.1"), D(4)),
        (D("200.1"), D(1)),
        (D("200.2"), D(3)),
    ]


def test_sell_market_order_walks_the_bids():
    sim = _ready()
    sim.submit(order("6", price=None, side=Side.SELL))
    fills = _fills(sim.on_event(book_event(300)))
    assert [(f.price, f.quantity) for f in fills] == [(D("199.9"), D(5)), (D("199.8"), D(1))]
    assert sim.position(19).quantity == -6


# --- Resting limits ---


def test_marketable_gtc_takes_what_it_can_and_rests_the_rest():
    sim = _ready()
    sim.submit(order("7", price="200.1"))
    fills = _fills(sim.on_event(book_event(300)))
    assert [(f.price, f.quantity, f.is_taker) for f in fills] == [(D("200.1"), D(5), True)]
    state = sim.order(_coid(0))
    assert (state.status, state.resting_quantity) == (OrderStatus.PARTIAL, 2)


def test_resting_limit_fills_only_on_a_trade_through_its_price():
    sim = _ready()
    sim.submit(order("2", price="199.5"))
    sim.on_event(book_event(300))
    assert sim.order(_coid(0)).status is OrderStatus.OPEN
    # Touching the limit is not enough.
    assert _fills(sim.on_event(trades_event(400, ("199.5", "10")))) == []
    fills = _fills(sim.on_event(trades_event(500, ("199.4", "0.5"), ("199.3", "5"))))
    assert [(f.price, f.quantity, f.is_taker) for f in fills] == [
        (D("199.5"), D("0.5"), False),
        (D("199.5"), D("1.5"), False),
    ]
    assert fills[0].fee == D("199.5") * D("0.5") * D("0.000125")
    assert sim.order(_coid(0)).status is OrderStatus.FILLED


def test_trades_before_the_order_reached_the_venue_do_not_fill_it():
    sim = _ready()
    sim.submit(order("1", price="199.5"))
    sim.on_event(trades_event(100, ("199", "5")))  # printed before arrival
    sim.on_event(book_event(300))
    assert _fills(sim.on_event(trades_event(100, ("199", "5")))) == []
    assert sim.order(_coid(0)).filled_quantity == 0


def test_a_trade_is_shared_across_resting_orders_best_price_first():
    sim = _ready()
    sim.submit(order("1", price="199.5"))
    sim.submit(order("1", price="199.6", client_order_id=_coid(1)))
    sim.on_event(book_event(300))
    fills = _fills(sim.on_event(trades_event(400, ("199.4", "1.5"))))
    assert [(f.client_order_id, f.quantity) for f in fills] == [(_coid(1), 1), (_coid(0), D("0.5"))]


def test_post_only_that_would_cross_is_rejected():
    sim = _ready()
    sim.submit(order("1", price="200.1", post_only=True))
    sim.submit(order("1", price="200", post_only=True, client_order_id=_coid(1)))
    sim.on_event(book_event(300))
    assert sim.order(_coid(0)).status is OrderStatus.POST_ONLY_REJECTED
    assert sim.order(_coid(1)).status is OrderStatus.OPEN


def test_cancel_and_cancel_all():
    sim = _ready()
    sim.submit(order("1", price="199"))
    sim.submit(order("1", price="198", client_order_id=_coid(1)))
    sim.on_event(book_event(300))
    assert sim.cancel(_coid(0)).status is OrderStatus.CANCELLED
    assert sim.open_orders() == (sim.order(_coid(1)),)
    sim.cancel(_coid(1))
    assert sim.open_orders() == ()
    with pytest.raises(RequestRejected):
        sim.cancel(_coid(9))


def test_async_gateway_and_reader():
    sim = _ready()

    async def scenario() -> None:
        await sim.place_order(order("1", price=None))
        sim.on_event(book_event(300))
        (position,) = await sim.fetch_positions()
        assert (position.quantity, position.isolated, position.leverage) == (1, True, 10)
        assert len(await sim.fetch_fills(T0, T0 + timedelta(seconds=1))) == 1
        await sim.place_order(order("1", price="199", client_order_id=_coid(1)))
        assert len(await sim.fetch_open_orders()) == 1
        assert len(await sim.cancel_all(19)) == 1
        account = await sim.fetch_account()
        assert account.taker_fee_rate == D("0.0004")

    asyncio.run(scenario())


# --- Admission ---


def test_admission_applies_venue_rules():
    sim, _ = simulator()
    assert sim.submit(order("1", price="200")).status is OrderStatus.MARK_PRICE_UNAVAILABLE
    sim.on_event(ticker_event(0))
    outside = sim.submit(order("1", price="250", client_order_id=_coid(1)))
    assert outside.status is OrderStatus.PRICE_OUTSIDE_BOUNDS
    small = sim.submit(order("0.01", price="200", client_order_id=_coid(2)))
    assert small.status is OrderStatus.BELOW_MIN_NOTIONAL
    with pytest.raises(RequestRejected, match="decimals"):
        sim.submit(order("1", price="200.001", client_order_id=_coid(3)))
    with pytest.raises(RequestRejected, match="duplicate"):
        sim.submit(order("1", price="200", client_order_id=_coid(1)))


def test_reduce_only_needs_a_position_to_reduce():
    sim = _ready()
    state = sim.submit(order("1", price=None, side=Side.SELL, reduce_only=True))
    assert state.status is OrderStatus.REDUCE_ONLY_INVALID


def test_reduce_only_never_fills_beyond_the_position():
    sim = _ready()
    sim.submit(order("2", price=None))
    sim.on_event(book_event(300))
    sim.submit(order("5", price=None, side=Side.SELL, reduce_only=True, client_order_id=_coid(1)))
    sim.on_event(book_event(600))
    assert sim.position(19) is None
    assert sim.order(_coid(1)).filled_quantity == 2


def test_resting_reduce_only_expires_when_the_position_closes():
    sim = _ready()
    sim.submit(order("2", price=None))
    sim.on_event(book_event(300))
    sim.submit(order("2", price="201", side=Side.SELL, reduce_only=True, client_order_id=_coid(1)))
    sim.submit(order("2", price=None, side=Side.SELL, reduce_only=True, client_order_id=_coid(2)))
    sim.on_event(book_event(600))
    assert sim.position(19) is None
    assert sim.order(_coid(1)).status is OrderStatus.REDUCE_ONLY_EXPIRED


def test_orders_need_margin_and_resting_orders_consume_it():
    sim = _ready()  # 10,000 collateral at 10x: 100,000 notional, 500 NVDA at 200
    sim.set_leverage(19, D(2))  # now 20,000 notional: 100 NVDA
    sim.submit(order("60", price="199"))
    with pytest.raises(RequestRejected, match="insufficient margin"):
        sim.submit(order("50", price="199", client_order_id=_coid(1)))
    # An order on the other side does not add to the worst case.
    sim.submit(order("50", price="201", side=Side.SELL, client_order_id=_coid(2)))
    assert sim.account_summary().available_margin == D(10_000) - D(60) * 200 / 2


def test_leverage_must_be_within_the_venue_cap():
    sim = _ready()
    with pytest.raises(RequestRejected):
        sim.set_leverage(19, D(11))


# --- Account ---


def test_position_accounting_averages_entries_and_realizes_on_reduce():
    sim = _ready()
    sim.submit(order("1", price=None))
    sim.on_event(book_event(300, asks=(("200", "10"),)))
    sim.submit(order("1", price=None, client_order_id=_coid(1)))
    sim.on_event(book_event(600, asks=(("202", "10"),)))
    assert sim.position(19).entry_price == 201
    sim.submit(order("3", price=None, side=Side.SELL, client_order_id=_coid(2)))
    events = sim.on_event(book_event(900, bids=(("205", "10"),)))
    realized = [e.realized_pnl for e in events if isinstance(e, FillEvent)]
    assert realized == [D(8)]  # 2 × (205 − 201); the third unit opens a short
    position = sim.position(19)
    assert (position.quantity, position.entry_price) == (-1, 205)
    fees = (D(200) + D(202) + D(615)) * D("0.0004")
    assert sim.account.collateral == D(10_000) + 8 - fees
    sim.on_event(ticker_event(1000, mark="204"))
    assert sim.account_summary().equity == sim.account.collateral + 1


def test_liquidation_price_matches_the_maintenance_margin_condition():
    sim = _ready()
    sim.submit(order("10", price=None))
    sim.on_event(book_event(300, asks=(("200", "10"),)))
    position = sim.position(19)
    # 10x: margin 200, MM 5%: liquidation where 200 + 10(p − 200) = 0.5 p.
    price = position.liquidation_price()
    assert price == D(1800) / D("9.5")
    assert not position.is_liquidatable(price + D("0.01"))
    assert position.is_liquidatable(price - D("0.01"))


# --- Funding ---


def test_funding_settles_once_at_next_funding_with_the_last_rate_and_mark():
    sim = _ready()
    sim.submit(order("10", price=None))
    sim.on_event(book_event(300, asks=(("200", "10"),)))
    boundary = T0 + timedelta(hours=1)
    sim.on_event(ticker_event(1000, mark="210", rate="0.0001", next_funding=boundary))
    after = 3_600_000 + 100
    events = sim.on_event(
        ticker_event(after, rate="0.0002", next_funding=boundary + timedelta(hours=1))
    )
    (payment,) = [e for e in events if isinstance(e, FundingPayment)]
    assert payment.amount == -10 * D(210) * D("0.0001")  # the long pays
    assert payment.timestamp == boundary
    assert sim.on_event(ticker_event(after + 100, next_funding=boundary + timedelta(hours=1))) == []
    assert sim.account.funding_paid == D("0.21")


def test_shorts_receive_positive_funding_and_flat_accounts_pay_nothing():
    sim = _ready()
    assert sim.settle_funding(19, D("0.0001"), at(0)) == []
    sim.submit(order("10", price=None, side=Side.SELL))
    sim.on_event(book_event(300, bids=(("200", "10"),)))
    (payment,) = sim.settle_funding(19, D("0.0001"), at(400))
    assert payment.amount == D("0.2")


# --- Position TP/SL ---


def test_stop_triggers_on_mark_and_closes_against_the_book():
    sim = _ready()
    sim.submit(order("2", price=None))
    sim.on_event(book_event(300))
    sim.set_protection(19, PositionProtection(stop_loss=D(195), take_profit=D(210)))
    # The book trades through the stop but mark does not: nothing happens.
    sim.on_event(book_event(400, bids=(("194", "5"),), asks=(("194.2", "5"),)))
    assert sim.position(19).quantity == 2
    events = sim.on_event(ticker_event(500, mark="194.9"))
    (trigger,) = [e for e in events if isinstance(e, ProtectionTriggered)]
    assert (trigger.kind, trigger.trigger_price) == ("stop_loss", 195)
    assert sim.protection(19) == PositionProtection()
    # The close reaches the venue after the latency and sells into the bids.
    assert _fills(sim.on_event(book_event(600, bids=(("194", "5"),)))) == []
    fills = _fills(sim.on_event(book_event(800, bids=(("193.5", "1"), ("193", "5")))))
    assert [(f.price, f.quantity) for f in fills] == [(D("193.5"), D(1)), (D("193"), D(1))]
    assert sim.position(19) is None
    assert sim.order(trigger.client_order_id).status is OrderStatus.FILLED


def test_triggered_close_keeps_sweeping_new_books_until_flat():
    sim = _ready()
    sim.submit(order("4", price=None, side=Side.SELL))
    sim.on_event(book_event(300))
    sim.set_protection(19, PositionProtection(take_profit=D(190)))
    (trigger,) = [
        e for e in sim.on_event(ticker_event(400, mark="189")) if isinstance(e, ProtectionTriggered)
    ]
    assert trigger.kind == "take_profit"
    sim.on_event(book_event(700, asks=(("189", "1"),)))
    assert sim.position(19).quantity == -3
    sim.on_event(book_event(800, asks=(("189.1", "5"),)))
    assert sim.position(19) is None


def test_protection_needs_a_position_and_clears_when_flat():
    sim = _ready()
    with pytest.raises(RequestRejected):
        sim.set_protection(19, PositionProtection(stop_loss=D(190)))
    sim.submit(order("1", price=None))
    sim.on_event(book_event(300))
    sim.set_protection(19, PositionProtection(stop_loss=D(190)))
    sim.submit(order("1", price=None, side=Side.SELL, client_order_id=_coid(1)))
    sim.on_event(book_event(600))
    assert sim.protection(19) == PositionProtection()


# --- Liquidation ---


def test_liquidation_sweeps_the_book_with_the_liquidation_fee(caplog):
    sim = _ready()
    sim.submit(order("10", price=None))
    sim.on_event(book_event(300, asks=(("200", "10"),)))
    sim.submit(order("1", price="185", client_order_id=_coid(1)))
    sim.on_event(book_event(400, bids=(("189", "4"),)))
    with caplog.at_level(logging.ERROR):
        events = sim.on_event(ticker_event(500, mark="189"))
    (liquidation,) = [e for e in events if isinstance(e, Liquidation)]
    assert liquidation.position_quantity == 10
    fills = _fills(events)
    assert all(f.liquidation for f in fills)
    # 4 into the bids, the 6 the book could not take at mark.
    assert [(f.price, f.quantity) for f in fills] == [(D(189), D(4)), (D(189), D(6))]
    assert fills[0].fee == D(189) * 4 * (D("0.0004") + D("0.005"))
    assert sim.position(19) is None
    assert sim.order(_coid(1)).status is OrderStatus.CANCELLED
    assert "simulated liquidation" in caplog.text


def test_events_are_returned_once_in_order():
    sim = _ready()
    sim.submit(order("1", price=None))
    events = sim.on_event(book_event(300))
    assert [type(e) for e in events] == [OrderUpdate, FillEvent, OrderUpdate]
    assert sim.drain() == []
