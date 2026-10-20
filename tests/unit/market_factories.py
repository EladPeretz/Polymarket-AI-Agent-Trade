"""Stream-event builders and a controllable clock for market unit tests."""

from datetime import datetime, timedelta
from decimal import Decimal

from perps_agent.config.models import MarketDataConfig
from perps_agent.venue import (
    Bbo,
    BboEvent,
    BookEvent,
    BookLevel,
    OrderBook,
    Side,
    TickerEvent,
    Trade,
    TradesEvent,
)
from tests.unit.venue_factories import T0, ticker

CONFIG = MarketDataConfig(stale_after_ms=2_000, resubscribe_after_ms=15_000)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


def book(
    at: datetime, bid: str = "199.9", ask: str = "200.1", instrument_id: int = 19
) -> OrderBook:
    return OrderBook(
        instrument_id=instrument_id,
        bids=(
            BookLevel(price=Decimal(bid), quantity=Decimal(5)),
            BookLevel(price=Decimal(bid) - 1, quantity=Decimal(10)),
        ),
        asks=(
            BookLevel(price=Decimal(ask), quantity=Decimal(5)),
            BookLevel(price=Decimal(ask) + 1, quantity=Decimal(10)),
        ),
        timestamp=at,
        sequence=1,
    )


def book_event(at: datetime, sequence: int = 1, **kwargs: object) -> BookEvent:
    return BookEvent(
        channel="book::19",
        sequence=sequence,
        received_at=at + timedelta(milliseconds=80),
        book=book(at, **kwargs),  # type: ignore[arg-type]
    )


def ticker_event(at: datetime, mark: str = "200", sequence: int = 1) -> TickerEvent:
    return TickerEvent(
        channel="tickers::19",
        sequence=sequence,
        received_at=at + timedelta(milliseconds=80),
        ticker=ticker(mark).model_copy(update={"timestamp": at}),
    )


def bbo_event(at: datetime) -> BboEvent:
    return BboEvent(
        channel="bbo::19",
        sequence=1,
        received_at=at,
        bbo=Bbo(
            instrument_id=19,
            bid_price=Decimal("199.9"),
            bid_quantity=Decimal(5),
            ask_price=Decimal("200.1"),
            ask_quantity=Decimal(5),
            timestamp=at,
            sequence=1,
        ),
    )


def trade(trade_id: int, at: datetime, price: str = "200") -> Trade:
    return Trade(
        trade_id=trade_id,
        instrument_id=19,
        aggressor=Side.BUY,
        price=Decimal(price),
        quantity=Decimal("0.1"),
        timestamp=at,
    )


def trades_event(at: datetime, *trades: Trade) -> TradesEvent:
    return TradesEvent(channel="trades::19", sequence=1, received_at=at, trades=trades)
