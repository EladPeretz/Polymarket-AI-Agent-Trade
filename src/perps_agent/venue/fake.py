"""In-memory fake exchange for tests: a ``MarketDataSource`` plus an ``OrderGateway``.

It never signs or sends anything. Tests set its market state directly and push
stream events into it. Orders are checked with the same venue rules as the real
adapter and rest until cancelled or filled by the test; matching and fill
modelling live in ``perps_agent.sim``.
"""

import asyncio
import itertools
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

from perps_agent.clock import utc_now
from perps_agent.venue.errors import RequestRejected, UnknownInstrument
from perps_agent.venue.interfaces import BookDepth, CandleInterval, MarketStream
from perps_agent.venue.models import (
    BboEvent,
    BookEvent,
    Candle,
    ExchangeStatus,
    Fill,
    FundingRate,
    Instrument,
    MarketEvent,
    MarkPoint,
    OrderBook,
    OrderRequest,
    OrderState,
    OrderStatus,
    StreamChannel,
    Ticker,
    TickerEvent,
    TimeInForce,
    Trade,
    TradesEvent,
)
from perps_agent.venue.rules import STATUS_FOR_VIOLATION, check_order


def _channel_of(event: MarketEvent) -> StreamChannel:
    match event:
        case BboEvent():
            return StreamChannel.BBO
        case BookEvent():
            return StreamChannel.BOOK
        case TradesEvent():
            return StreamChannel.TRADES
        case TickerEvent():
            return StreamChannel.TICKER


class _FakeStream:
    def __init__(
        self, owner: "FakeExchange", ids: frozenset[int], channels: frozenset[StreamChannel]
    ) -> None:
        self._owner = owner
        self.instrument_ids = ids
        self.channels = channels
        self._queue: asyncio.Queue[MarketEvent | None] = asyncio.Queue()

    def wants(self, instrument_id: int, channel: StreamChannel) -> bool:
        return instrument_id in self.instrument_ids and channel in self.channels

    def put(self, event: MarketEvent | None) -> None:
        self._queue.put_nowait(event)

    def __aiter__(self) -> AsyncIterator[MarketEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[MarketEvent]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def close(self) -> None:
        self._owner._streams.discard(self)
        self.put(None)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


class FakeExchange:
    """Satisfies ``MarketDataSource`` and ``OrderGateway``. All state is in memory."""

    def __init__(self, instruments: Sequence[Instrument] = ()) -> None:
        self.status = ExchangeStatus(engine_version="fake", cancel_only=False)
        self.instruments: dict[int, Instrument] = {i.instrument_id: i for i in instruments}
        self.tickers: dict[int, Ticker] = {}
        self.books: dict[int, OrderBook] = {}
        self.trades: dict[int, list[Trade]] = {}
        self.candles: dict[tuple[int, str], list[Candle]] = {}
        self.marks: dict[tuple[int, str], list[MarkPoint]] = {}
        self.funding: dict[int, list[FundingRate]] = {}
        self.orders: dict[str, OrderState] = {}
        self.fills: list[Fill] = []
        self._streams: set[_FakeStream] = set()
        self._order_ids = itertools.count(1)
        self._trade_ids = itertools.count(1)

    # --- MarketDataSource ---

    async def aclose(self) -> None:
        for stream in list(self._streams):
            await stream.close()

    async def fetch_exchange_status(self) -> ExchangeStatus:
        return self.status

    async def fetch_instruments(self) -> tuple[Instrument, ...]:
        return tuple(self.instruments.values())

    async def fetch_tickers(self) -> tuple[Ticker, ...]:
        return tuple(self.tickers.values())

    async def fetch_ticker(self, instrument_id: int) -> Ticker:
        return self.tickers[self._known(instrument_id)]

    async def fetch_book(self, instrument_id: int, depth: BookDepth = 100) -> OrderBook:
        book = self.books[self._known(instrument_id)]
        return book.model_copy(update={"bids": book.bids[:depth], "asks": book.asks[:depth]})

    async def fetch_trades(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> tuple[Trade, ...]:
        rows = self.trades.get(self._known(instrument_id), [])
        return tuple(t for t in rows if start <= t.timestamp <= end)

    async def fetch_candles(
        self, instrument_id: int, interval: CandleInterval, start: datetime, end: datetime
    ) -> tuple[Candle, ...]:
        rows = self.candles.get((self._known(instrument_id), interval), [])
        return tuple(c for c in rows if start <= c.open_time <= end)

    async def fetch_mark_history(
        self, instrument_id: int, interval: CandleInterval, start: datetime, end: datetime
    ) -> tuple[MarkPoint, ...]:
        rows = self.marks.get((self._known(instrument_id), interval), [])
        return tuple(m for m in rows if start <= m.timestamp <= end)

    async def fetch_funding_history(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> tuple[FundingRate, ...]:
        rows = self.funding.get(self._known(instrument_id), [])
        return tuple(f for f in rows if start <= f.timestamp <= end)

    async def stream(
        self, instrument_ids: Sequence[int], channels: Sequence[StreamChannel]
    ) -> MarketStream:
        for iid in instrument_ids:
            self._known(iid)
        stream = _FakeStream(self, frozenset(instrument_ids), frozenset(channels))
        self._streams.add(stream)
        return stream

    # --- Test controls ---

    def publish(self, event: MarketEvent, instrument_id: int) -> None:
        """Deliver ``event`` to every open stream subscribed to its instrument and channel."""
        channel = _channel_of(event)
        for stream in list(self._streams):
            if stream.wants(instrument_id, channel):
                stream.put(event)

    def fill(self, client_order_id: str, price: Decimal, quantity: Decimal) -> Fill:
        """Fill (part of) a resting order, as a test scenario dictates."""
        state = self.orders[client_order_id]
        if not state.status.is_live:
            raise RequestRejected(f"order {client_order_id} is {state.status}", status=400)
        if not 0 < quantity <= state.resting_quantity:
            raise ValueError(f"fill quantity {quantity} not in (0, {state.resting_quantity}]")
        now = utc_now()
        filled = state.filled_quantity + quantity
        resting = state.resting_quantity - quantity
        self.orders[client_order_id] = state.model_copy(
            update={
                "filled_quantity": filled,
                "resting_quantity": resting,
                "status": OrderStatus.FILLED if resting == 0 else OrderStatus.PARTIAL,
                "updated_at": now,
            }
        )
        fill = Fill(
            trade_id=next(self._trade_ids),
            order_id=state.order_id,
            client_order_id=client_order_id,
            instrument_id=state.request.instrument_id,
            side=state.request.side,
            price=price,
            quantity=quantity,
            fee=Decimal(0),
            is_taker=False,
            timestamp=now,
        )
        self.fills.append(fill)
        return fill

    # --- OrderGateway ---

    async def place_order(self, request: OrderRequest) -> OrderState:
        if request.client_order_id in self.orders:
            raise RequestRejected("duplicate client order id", status=400, code="duplicate_order")
        instrument = self.instruments[self._known(request.instrument_id)]
        if self.status.cancel_only:
            raise RequestRejected("exchange is cancel-only", status=403, code="cancel_only_mode")
        status = OrderStatus.OPEN
        ticker = self.tickers.get(request.instrument_id)
        if ticker is None:
            status = OrderStatus.MARK_PRICE_UNAVAILABLE
        else:
            for violation in check_order(instrument, request, ticker.mark_price):
                mapped = STATUS_FOR_VIOLATION.get(violation.rule)
                if mapped is None:
                    raise RequestRejected(violation.detail, status=400, code=violation.rule)
                status = mapped
                break
        # No matching in the fake: an IOC/FOK has nothing to hit and expires unfilled.
        if status is OrderStatus.OPEN and request.time_in_force is TimeInForce.IOC:
            status = OrderStatus.IOC_NO_FILL
        elif status is OrderStatus.OPEN and request.time_in_force is TimeInForce.FOK:
            status = OrderStatus.FOK_UNFILLED
        now = utc_now()
        state = OrderState(
            order_id=next(self._order_ids),
            request=request,
            status=status,
            filled_quantity=Decimal(0),
            resting_quantity=request.quantity if status is OrderStatus.OPEN else Decimal(0),
            created_at=now,
            updated_at=now,
        )
        self.orders[request.client_order_id] = state
        return state

    async def cancel_order(self, client_order_id: str) -> OrderState:
        state = self.orders.get(client_order_id)
        if state is None:
            raise RequestRejected("order not found", status=404, code="order_not_found")
        if not state.status.is_live:
            return state
        cancelled = state.model_copy(
            update={
                "status": OrderStatus.CANCELLED,
                "resting_quantity": Decimal(0),
                "updated_at": utc_now(),
            }
        )
        self.orders[client_order_id] = cancelled
        return cancelled

    async def cancel_all(self, instrument_id: int | None = None) -> tuple[OrderState, ...]:
        live = [
            coid
            for coid, state in self.orders.items()
            if state.status.is_live
            and (instrument_id is None or state.request.instrument_id == instrument_id)
        ]
        return tuple([await self.cancel_order(coid) for coid in live])

    def _known(self, instrument_id: int) -> int:
        if instrument_id not in self.instruments:
            raise UnknownInstrument(f"no instrument with id {instrument_id}")
        return instrument_id
