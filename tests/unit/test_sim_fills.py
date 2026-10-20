from decimal import Decimal

from perps_agent.sim.fills import Execution, Level, available, fee, trades_through, walk_book
from perps_agent.venue import Side


def _asks() -> list[Level]:
    return [Level(Decimal("100.1"), Decimal(2)), Level(Decimal("100.2"), Decimal(3))]


def test_walk_book_takes_levels_in_order_and_consumes_them():
    asks = _asks()
    executions = walk_book(asks, Side.BUY, Decimal(4), None)
    assert executions == [
        Execution(Decimal("100.1"), Decimal(2)),
        Execution(Decimal("100.2"), Decimal(2)),
    ]
    assert asks == [Level(Decimal("100.2"), Decimal(1))]


def test_walk_book_stops_at_the_limit_and_when_the_book_runs_out():
    assert walk_book(_asks(), Side.BUY, Decimal(4), Decimal("100.1")) == [
        Execution(Decimal("100.1"), Decimal(2))
    ]
    assert sum(e.quantity for e in walk_book(_asks(), Side.BUY, Decimal(50), None)) == 5


def test_sell_walks_bids_down_to_its_limit():
    bids = [Level(Decimal("99.9"), Decimal(1)), Level(Decimal("99.8"), Decimal(1))]
    assert walk_book(bids, Side.SELL, Decimal(2), Decimal("99.85")) == [
        Execution(Decimal("99.9"), Decimal(1))
    ]


def test_available_within_limit():
    assert available(_asks(), Side.BUY, Decimal("100.15")) == 2
    assert available(_asks(), Side.BUY, None) == 5


def test_trade_must_print_through_not_touch():
    assert trades_through(Side.BUY, Decimal(100), Decimal("99.99"))
    assert not trades_through(Side.BUY, Decimal(100), Decimal(100))
    assert trades_through(Side.SELL, Decimal(100), Decimal("100.01"))
    assert not trades_through(Side.SELL, Decimal(100), Decimal(100))


def test_fee_is_notional_times_rate():
    assert fee(Decimal(200), Decimal("1.5"), Decimal("0.0004")) == Decimal("0.12")
