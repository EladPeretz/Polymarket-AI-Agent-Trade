"""Venue interfaces (ADR-0001). Everything outside ``venue`` depends on these, not the SDK."""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from types import TracebackType
from typing import Literal, Protocol, Self

from perps_agent.venue.models import (
    AccountSummary,
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
    Position,
    StreamChannel,
    Ticker,
    Trade,
)

BookDepth = Literal[10, 100, 500, 1000]
# 1s: mark history only (the backtest builds minute bars from it).
CandleInterval = Literal["1s", "1m", "5m", "15m", "1h", "4h", "1d", "1w"]


class MarketStream(Protocol):
    """An open market-data subscription. Iterate it for events; close it when done.

    The SDK reconnects underneath and does not report it. ``sequence`` cannot
    reveal a reconnect either (see ``_StreamEvent``), so the consumer (WP3) must
    detect staleness from time and re-snapshot via REST.
    """

    def __aiter__(self) -> AsyncIterator[MarketEvent]: ...

    async def close(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class MarketDataSource(Protocol):
    """Public market data. Needs no credentials. Call ``aclose`` when done."""

    async def aclose(self) -> None: ...

    async def fetch_exchange_status(self) -> ExchangeStatus: ...

    async def fetch_instruments(self) -> tuple[Instrument, ...]: ...

    async def fetch_ticker(self, instrument_id: int) -> Ticker: ...

    async def fetch_tickers(self) -> tuple[Ticker, ...]: ...

    async def fetch_book(self, instrument_id: int, depth: BookDepth = 100) -> OrderBook: ...

    async def fetch_trades(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> tuple[Trade, ...]: ...

    async def fetch_candles(
        self, instrument_id: int, interval: CandleInterval, start: datetime, end: datetime
    ) -> tuple[Candle, ...]: ...

    async def fetch_mark_history(
        self, instrument_id: int, interval: CandleInterval, start: datetime, end: datetime
    ) -> tuple[MarkPoint, ...]: ...

    async def fetch_funding_history(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> tuple[FundingRate, ...]: ...

    async def stream(
        self, instrument_ids: Sequence[int], channels: Sequence[StreamChannel]
    ) -> MarketStream: ...


class AccountReader(Protocol):
    """Private account state. Implemented for live mode only (after paper trading)."""

    async def fetch_account(self) -> AccountSummary: ...

    async def fetch_positions(self) -> tuple[Position, ...]: ...

    async def fetch_open_orders(self) -> tuple[OrderState, ...]: ...

    async def fetch_fills(self, start: datetime, end: datetime) -> tuple[Fill, ...]: ...


class OrderGateway(Protocol):
    """Order entry. Paper mode uses the simulator; the live gateway comes after paper
    trading meets its gate, and only runs on the approved VPS (ADR-0004).

    Every request carries a 32-hex ``client_order_id`` so outcomes can be reconciled.
    """

    async def place_order(self, request: OrderRequest) -> OrderState: ...

    async def cancel_order(self, client_order_id: str) -> OrderState: ...

    async def cancel_all(self, instrument_id: int | None = None) -> tuple[OrderState, ...]: ...
