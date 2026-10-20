import asyncio
from decimal import Decimal

import pytest

from perps_agent.venue import (
    BboEvent,
    MarketDataSource,
    OrderGateway,
    OrderStatus,
    RequestRejected,
    Side,
    StreamChannel,
    TickerEvent,
)
from perps_agent.venue.fake import FakeExchange
from perps_agent.venue.models import Bbo, ExchangeStatus
from tests.unit.venue_factories import T0, instrument, order, ticker


def exchange() -> FakeExchange:
    fake = FakeExchange([instrument()])
    fake.tickers[19] = ticker("200")
    return fake


def test_satisfies_the_interfaces() -> None:
    fake = exchange()
    source: MarketDataSource = fake
    gateway: OrderGateway = fake
    assert asyncio.run(source.fetch_ticker(19)).mark_price == Decimal(200)
    assert gateway is fake


def test_valid_order_rests_and_can_be_filled_and_cancelled() -> None:
    fake = exchange()
    state = asyncio.run(fake.place_order(order(quantity="2", coid="a" * 32)))
    assert state.status is OrderStatus.OPEN and state.resting_quantity == 2

    fill = fake.fill("a" * 32, Decimal("199.99"), Decimal("0.5"))
    assert fill.side is Side.BUY and fill.client_order_id == "a" * 32
    assert fake.orders["a" * 32].status is OrderStatus.PARTIAL
    assert fake.orders["a" * 32].resting_quantity == Decimal("1.5")

    cancelled = asyncio.run(fake.cancel_order("a" * 32))
    assert cancelled.status is OrderStatus.CANCELLED and cancelled.resting_quantity == 0
    assert cancelled.filled_quantity == Decimal("0.5")


def test_venue_rules_reject_like_the_venue() -> None:
    fake = exchange()
    small = asyncio.run(fake.place_order(order(quantity="0.01", coid="b" * 32)))
    assert small.status is OrderStatus.BELOW_MIN_NOTIONAL
    outside = asyncio.run(fake.place_order(order(price="250", coid="c" * 32)))
    assert outside.status is OrderStatus.PRICE_OUTSIDE_BOUNDS
    with pytest.raises(RequestRejected):
        asyncio.run(fake.place_order(order(price="200.001", coid="d" * 32)))
    with pytest.raises(RequestRejected):
        asyncio.run(fake.place_order(order(coid="b" * 32)))  # duplicate client order id


def test_ioc_without_matching_expires() -> None:
    fake = exchange()
    state = asyncio.run(fake.place_order(order(price=None, coid="e" * 32)))
    assert state.status is OrderStatus.IOC_NO_FILL and state.resting_quantity == 0


def test_cancel_only_and_missing_mark() -> None:
    fake = exchange()
    fake.status = ExchangeStatus(engine_version="fake", cancel_only=True)
    with pytest.raises(RequestRejected):
        asyncio.run(fake.place_order(order()))
    fake = FakeExchange([instrument()])
    assert asyncio.run(fake.place_order(order())).status is OrderStatus.MARK_PRICE_UNAVAILABLE


def test_cancel_all_by_instrument() -> None:
    fake = exchange()
    for coid in ("1" * 32, "2" * 32):
        asyncio.run(fake.place_order(order(coid=coid)))
    cancelled = asyncio.run(fake.cancel_all(19))
    assert {s.status for s in cancelled} == {OrderStatus.CANCELLED}
    assert asyncio.run(fake.cancel_all()) == ()


def test_stream_delivers_subscribed_events_only() -> None:
    fake = exchange()
    bbo = BboEvent(
        channel="bbo::19",
        sequence=1,
        received_at=T0,
        bbo=Bbo(
            instrument_id=19,
            bid_price=Decimal(199),
            bid_quantity=Decimal(1),
            ask_price=Decimal(201),
            ask_quantity=Decimal(1),
            timestamp=T0,
            sequence=1,
        ),
    )
    tick = TickerEvent(channel="tickers::19", sequence=1, received_at=T0, ticker=ticker())

    async def run():
        stream = await fake.stream([19], [StreamChannel.BBO])
        fake.publish(tick, 19)  # not subscribed to tickers
        fake.publish(bbo, 19)
        await stream.close()
        return [event async for event in stream]

    assert asyncio.run(run()) == [bbo]
