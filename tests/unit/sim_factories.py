"""Builders for simulator unit tests: a simulator with a controllable clock and market events."""

from datetime import datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

from perps_agent.config.models import SimConfig
from perps_agent.sim import PaperSimulator, ReplayClock
from perps_agent.venue import (
    BookEvent,
    BookLevel,
    OrderBook,
    Side,
    Ticker,
    TickerEvent,
    Trade,
    TradesEvent,
)
from tests.unit.venue_factories import T0, instrument

CONFIG = SimConfig(latency_ms=250, starting_collateral=Decimal(10_000))
_ids = count(1)


def simulator(**config: Any) -> tuple[PaperSimulator, ReplayClock]:
    clock = ReplayClock(T0)
    sim = PaperSimulator([instrument()], CONFIG.model_copy(update=config), clock=clock)
    return sim, clock


def at(ms: int) -> datetime:
    return T0 + timedelta(milliseconds=ms)


def levels(*pairs: tuple[str, str]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in pairs)


def book_event(
    ms: int,
    bids: tuple[tuple[str, str], ...] = (("199.9", "5"), ("199.8", "10")),
    asks: tuple[tuple[str, str], ...] = (("200.1", "5"), ("200.2", "10")),
) -> BookEvent:
    return BookEvent(
        channel="book::19",
        sequence=ms,
        received_at=at(ms),
        book=OrderBook(
            instrument_id=19, bids=levels(*bids), asks=levels(*asks), timestamp=at(ms), sequence=ms
        ),
    )


def ticker_event(
    ms: int,
    mark: str = "200",
    rate: str = "0.00000625",
    next_funding: datetime | None = None,
) -> TickerEvent:
    price = Decimal(mark)
    return TickerEvent(
        channel="tickers::19",
        sequence=ms,
        received_at=at(ms),
        ticker=Ticker(
            instrument_id=19,
            mark_price=price,
            index_price=price,
            last_price=price,
            mid_price=price,
            open_interest=Decimal(1000),
            funding_rate=Decimal(rate),
            next_funding=next_funding or T0 + timedelta(hours=1),
            timestamp=at(ms),
        ),
    )


def trades_event(ms: int, *prints: tuple[str, str]) -> TradesEvent:
    return TradesEvent(
        channel="trades::19",
        sequence=ms,
        received_at=at(ms),
        trades=tuple(
            Trade(
                trade_id=next(_ids),
                instrument_id=19,
                aggressor=Side.SELL,
                price=Decimal(price),
                quantity=Decimal(quantity),
                timestamp=at(ms),
            )
            for price, quantity in prints
        ),
    )
