"""``MarketDataSource`` over the pinned ``polymarket-client`` SDK.

This module and nothing else imports the SDK (ADR-0001). It maps SDK models to
our domain models, translates SDK errors into :mod:`perps_agent.venue.errors`,
retries reads with bounded backoff, and works around known SDK/API issues:

* ``GET /v1/info/tickers`` ignores ``instrument_id`` and the SDK's
  ``fetch_perps_ticker`` returns the first row (SP500), not the instrument asked
  for. We fetch all tickers and filter by ``instrument_id`` ourselves.
* The SDK parses epoch milliseconds through a float; timestamps are rounded
  back to whole milliseconds here.
* Mark-price history and exchange status are not in the SDK, so they are read
  with plain HTTP GETs.

Read-only: it holds no credentials and has no path that signs or sends an order.
"""

import dataclasses
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import httpx
import pydantic
from polymarket import AsyncPublicClient
from polymarket._internal.environment import get_environment_config, with_environment_config
from polymarket.clients._transport import AsyncTransport
from polymarket.environments import PRODUCTION
from polymarket.errors import (
    PolymarketError,
    RateLimitError,
    RequestRejectedError,
    TransportError,
    UnexpectedResponseError,
    UserInputError,
)
from polymarket.models.perps import (
    PerpsBook,
    PerpsBookLevel,
    PerpsCandle,
    PerpsFundingRate,
    PerpsInstrument,
    PerpsTicker,
    PerpsTrade,
)
from polymarket.models.perps.events import (
    PerpsBboEvent,
    PerpsBookEvent,
    PerpsMarketEvent,
    PerpsTickerEvent,
    PerpsTradeEvent,
    parse_perps_market_events,
)
from polymarket.streams import PerpsBboSpec, PerpsBookSpec, PerpsTickersSpec, PerpsTradesSpec
from polymarket.streams._specs import PerpsSpec

from perps_agent.clock import from_ms, to_ms, utc_now
from perps_agent.venue.errors import (
    RateLimited,
    RequestRejected,
    SchemaMismatch,
    VenueError,
    VenueUnavailable,
)
from perps_agent.venue.interfaces import BookDepth, CandleInterval, MarketStream
from perps_agent.venue.models import (
    Bbo,
    BboEvent,
    BookEvent,
    BookLevel,
    Candle,
    ExchangeStatus,
    FundingRate,
    Instrument,
    InstrumentCategory,
    MarketEvent,
    MarkPoint,
    OrderBook,
    RiskTier,
    Side,
    StreamChannel,
    Ticker,
    TickerEvent,
    Trade,
    TradesEvent,
)
from perps_agent.venue.retry import RetryPolicy, with_retries

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_INTERVAL_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}
_MAX_PAGES = 1_000


# --- SDK → domain mapping ---------------------------------------------------------


def _ms(value: datetime) -> datetime:
    """Round an SDK datetime (parsed via float) back to whole milliseconds, in UTC."""
    delta = value - _EPOCH
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return from_ms((micros + 500) // 1_000)


def _levels(levels: Sequence[PerpsBookLevel]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price=lvl.price, quantity=lvl.quantity) for lvl in levels)


def _side(value: str) -> Side:
    # The venue names trade sides by the aggressor's position direction.
    if value == "long":
        return Side.BUY
    if value == "short":
        return Side.SELL
    raise SchemaMismatch(f"unknown trade side {value!r}")


def map_instrument(raw: PerpsInstrument) -> Instrument:
    return Instrument(
        instrument_id=raw.id,
        symbol=raw.symbol,
        category=InstrumentCategory.parse(raw.category),
        base_asset=raw.base_asset,
        quote_asset=raw.quote_asset,
        price_decimals=raw.price_decimals,
        quantity_decimals=raw.quantity_decimals,
        price_band=raw.price_bounds,
        min_notional=raw.min_notional,
        max_market_notional=raw.max_market_notional,
        max_limit_notional=raw.max_limit_notional,
        max_leverage=raw.max_leverage,
        risk_tiers=tuple(
            RiskTier(notional_floor=tier.lower_bound, max_leverage=tier.max_leverage)
            for tier in raw.risk_tiers
        ),
        max_order_count=raw.max_order_count,
        liquidation_fee_rate=raw.liquidation_fee,
        funding_interval=raw.funding_interval,
        isolated_only=raw.isolated_only,
        close_only=raw.close_only,
        settled=raw.settlement is not None,
    )


def map_ticker(raw: PerpsTicker, received_at: datetime) -> Ticker:
    return Ticker(
        instrument_id=raw.instrument_id,
        mark_price=raw.mark_price,
        index_price=raw.index_price,
        last_price=raw.last_price,
        mid_price=raw.mid_price,
        open_interest=raw.open_interest,
        funding_rate=raw.funding_rate,
        next_funding=_ms(raw.next_funding),
        # Older responses carry no timestamp; fall back to when we received it.
        timestamp=_ms(raw.timestamp) if raw.timestamp is not None else received_at,
        open_price_24h=raw.open_price,
        volume_24h=raw.volume_24h,
    )


def map_book(raw: PerpsBook) -> OrderBook:
    return OrderBook(
        instrument_id=raw.instrument_id,
        bids=_levels(raw.bids),
        asks=_levels(raw.asks),
        timestamp=_ms(raw.timestamp),
        sequence=raw.sequence,
    )


def map_trade(raw: PerpsTrade) -> Trade:
    return Trade(
        trade_id=raw.trade_id,
        instrument_id=raw.instrument_id,
        aggressor=_side(raw.side),
        price=raw.price,
        quantity=raw.quantity,
        timestamp=_ms(raw.timestamp),
        settlement=raw.settlement,
    )


def map_candle(raw: PerpsCandle, instrument_id: int, interval: str) -> Candle:
    return Candle(
        instrument_id=instrument_id,
        interval=interval,
        open_time=_ms(raw.timestamp),
        open=raw.open,
        high=raw.high,
        low=raw.low,
        close=raw.close,
        volume=raw.volume,
        trade_count=raw.trades,
    )


def map_funding(raw: PerpsFundingRate, instrument_id: int) -> FundingRate:
    return FundingRate(
        instrument_id=instrument_id, rate=raw.funding_rate, timestamp=_ms(raw.timestamp)
    )


def map_market_event(raw: PerpsMarketEvent, received_at: datetime) -> MarketEvent | None:
    """Map one SDK stream event. Statistics and candle events are not used (``None``)."""
    timestamp = _ms(raw.timestamp)
    envelope: dict[str, Any] = {
        "channel": raw.channel,
        "sequence": raw.sequence,
        "received_at": received_at,
    }
    if isinstance(raw, PerpsBboEvent):
        p = raw.payload
        bbo = Bbo(
            instrument_id=p.instrument_id,
            bid_price=p.bid_price,
            bid_quantity=p.bid_quantity,
            ask_price=p.ask_price,
            ask_quantity=p.ask_quantity,
            timestamp=_ms(p.timestamp) if p.timestamp is not None else timestamp,
            sequence=raw.sequence,
        )
        return BboEvent(bbo=bbo, **envelope)
    if isinstance(raw, PerpsBookEvent):
        book = OrderBook(
            instrument_id=raw.payload.instrument_id,
            bids=_levels(raw.payload.bids),
            asks=_levels(raw.payload.asks),
            timestamp=timestamp,
            sequence=raw.sequence,
        )
        return BookEvent(book=book, **envelope)
    if isinstance(raw, PerpsTradeEvent):
        return TradesEvent(trades=tuple(map_trade(t) for t in raw.payload), **envelope)
    if isinstance(raw, PerpsTickerEvent):
        tp = raw.payload
        ticker = Ticker(
            instrument_id=tp.instrument_id,
            mark_price=tp.mark_price,
            index_price=tp.index_price,
            last_price=tp.last_price,
            mid_price=tp.mid_price,
            open_interest=tp.open_interest,
            funding_rate=tp.funding_rate,
            next_funding=_ms(tp.next_funding),
            timestamp=timestamp,
        )
        return TickerEvent(ticker=ticker, **envelope)
    return None


def event_instrument_id(event: MarketEvent) -> int:
    match event:
        case BboEvent():
            return event.bbo.instrument_id
        case BookEvent():
            return event.book.instrument_id
        case TickerEvent():
            return event.ticker.instrument_id
        case TradesEvent():
            return event.trades[0].instrument_id if event.trades else -1


def parse_frame(raw: object, received_at: datetime | None = None) -> tuple[MarketEvent, ...]:
    """Parse one decoded WebSocket frame into domain events (used by tests and the recorder).

    Frames that are not channel updates (acks) yield nothing; malformed updates
    are dropped and logged, as the SDK stream does.
    """
    events, dropped = parse_perps_market_events(raw)
    if dropped:
        log.warning("dropped malformed market frame(s)", extra={"dropped": dropped})
    at = received_at or utc_now()
    mapped = (map_market_event(event, at) for event in events)
    return tuple(event for event in mapped if event is not None)


# --- Error translation ------------------------------------------------------------


@contextmanager
def _translated(operation: str) -> Iterator[None]:
    try:
        yield
    except VenueError:
        raise
    except RateLimitError as error:
        raise RateLimited(f"{operation}: rate limited", retry_after=error.retry_after) from error
    except RequestRejectedError as error:
        if error.status == 429 or error.code == "ip_rate_limited":
            raise RateLimited(
                f"{operation}: rate limited", retry_after=error.retry_after
            ) from error
        if error.status >= 500:
            raise VenueUnavailable(f"{operation}: HTTP {error.status}: {error}") from error
        raise RequestRejected(
            f"{operation}: HTTP {error.status}: {error}", status=error.status, code=error.code
        ) from error
    except TransportError as error:
        raise VenueUnavailable(f"{operation}: {error}") from error
    except (UnexpectedResponseError, pydantic.ValidationError, KeyError, TypeError) as error:
        raise SchemaMismatch(f"{operation}: unexpected response: {error}") from error
    except UserInputError as error:
        raise ValueError(f"{operation}: {error}") from error
    except PolymarketError as error:
        raise VenueError(f"{operation}: {error}") from error


def _raise_for_status(response: httpx.Response, operation: str) -> None:
    if response.is_success:
        return
    code: str | None = None
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            code = body["error"]
    except ValueError:
        pass
    status = response.status_code
    if status == 429 or code == "ip_rate_limited":
        retry_after = response.headers.get("retry-after")
        hint = float(retry_after) if retry_after and retry_after.isdigit() else None
        raise RateLimited(f"{operation}: rate limited", retry_after=hint)
    if status >= 500:
        raise VenueUnavailable(f"{operation}: HTTP {status}")
    raise RequestRejected(f"{operation}: HTTP {status} {code or ''}", status=status, code=code)


# --- Data source ------------------------------------------------------------------


def _build_sdk_client(api_url: str, ws_url: str, http: httpx.AsyncClient) -> AsyncPublicClient:
    """An SDK client pointed at our configured endpoints, sending REST through ``http``."""
    config = dataclasses.replace(
        get_environment_config(PRODUCTION), perps_url=api_url, perps_ws_url=ws_url
    )
    client = AsyncPublicClient(with_environment_config(PRODUCTION, config=config))
    # Route the SDK's Perps REST calls through our own httpx client, so contract
    # tests can serve recorded responses and one connection pool is shared.
    object.__setattr__(client._ctx, "perps", AsyncTransport(base_url=api_url, client=http))
    return client


class PolymarketMarketData:
    """Public market data from Polymarket Perps. Use as an async context manager."""

    def __init__(
        self,
        *,
        api_url: str,
        ws_url: str,
        http_transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=api_url, timeout=timeout_seconds, transport=http_transport
        )
        self._sdk = _build_sdk_client(api_url, ws_url, self._http)
        self._retry = retry or RetryPolicy()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        try:
            await self._sdk.close()
        finally:
            await self._http.aclose()

    async def _read[T](self, operation: str, call: Callable[[], Awaitable[T]]) -> T:
        async def attempt() -> T:
            with _translated(operation):
                return await call()

        return await with_retries(attempt, self._retry, operation=operation)

    async def _get_json(self, operation: str, path: str, params: dict[str, Any]) -> Any:
        async def call() -> Any:
            try:
                response = await self._http.get(path, params=params)
            except httpx.HTTPError as error:
                raise VenueUnavailable(f"{operation}: {error}") from error
            _raise_for_status(response, operation)
            try:
                return response.json()
            except ValueError as error:
                raise SchemaMismatch(f"{operation}: response is not JSON") from error

        return await self._read(operation, call)

    async def fetch_exchange_status(self) -> ExchangeStatus:
        payload = await self._get_json("exchange status", "/v1/info/exchange", {})
        with _translated("exchange status"):
            return ExchangeStatus(
                engine_version=str(payload["engine_version"]),
                cancel_only=bool(payload["cancel_only"]),
            )

    async def fetch_instruments(self) -> tuple[Instrument, ...]:
        raw = await self._read("instruments", self._sdk.fetch_perps_instruments)
        with _translated("instruments"):
            return tuple(map_instrument(i) for i in raw)

    async def fetch_tickers(self) -> tuple[Ticker, ...]:
        raw = await self._read("tickers", self._sdk.fetch_perps_tickers)
        received_at = utc_now()
        with _translated("tickers"):
            return tuple(map_ticker(t, received_at) for t in raw)

    async def fetch_ticker(self, instrument_id: int) -> Ticker:
        # Never use the SDK's fetch_perps_ticker: it returns the wrong instrument (§14).
        for ticker in await self.fetch_tickers():
            if ticker.instrument_id == instrument_id:
                return ticker
        raise SchemaMismatch(f"ticker for instrument {instrument_id} missing from /tickers")

    async def fetch_book(self, instrument_id: int, depth: BookDepth = 100) -> OrderBook:
        raw = await self._read(
            "book", lambda: self._sdk.fetch_perps_book(instrument_id=instrument_id, depth=depth)
        )
        with _translated("book"):
            book = map_book(raw)
        if book.instrument_id != instrument_id:
            raise SchemaMismatch(f"asked for book {instrument_id}, got {book.instrument_id}")
        return book

    async def fetch_trades(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> tuple[Trade, ...]:
        async def call() -> list[PerpsTrade]:
            paginator = self._sdk.list_perps_trades(
                instrument_id=instrument_id, start=to_ms(start), end=to_ms(end)
            )
            return [t async for t in _bounded(paginator.iter_items())]

        raw = await self._read("trades", call)
        with _translated("trades"):
            trades = [map_trade(t) for t in raw]
        _check_ids("trades", instrument_id, (t.instrument_id for t in trades))
        return tuple(sorted(trades, key=lambda t: (t.timestamp, t.trade_id)))

    async def fetch_candles(
        self, instrument_id: int, interval: CandleInterval, start: datetime, end: datetime
    ) -> tuple[Candle, ...]:
        async def call() -> list[PerpsCandle]:
            paginator = self._sdk.list_perps_candles(
                instrument_id=instrument_id, interval=interval, start=to_ms(start), end=to_ms(end)
            )
            return [c async for c in _bounded(paginator.iter_items())]

        raw = await self._read("candles", call)
        with _translated("candles"):
            candles = [map_candle(c, instrument_id, interval) for c in raw]
        return tuple(sorted(candles, key=lambda c: c.open_time))

    async def fetch_mark_history(
        self, instrument_id: int, interval: CandleInterval, start: datetime, end: datetime
    ) -> tuple[MarkPoint, ...]:
        points: dict[int, MarkPoint] = {}
        cursor = to_ms(start)
        end_ms = to_ms(end)
        for _ in range(_MAX_PAGES):
            payload = await self._get_json(
                "mark history",
                "/v1/info/mark-history",
                {
                    "instrument_id": instrument_id,
                    "interval": interval,
                    "start_timestamp": cursor,
                    "end_timestamp": end_ms,
                },
            )
            with _translated("mark history"):
                rows = payload["data"]
                more = payload["more"]
                if not isinstance(rows, list) or not isinstance(more, bool):
                    raise SchemaMismatch("mark history: unexpected envelope")
                for ts, price in rows:
                    if not isinstance(ts, int) or not isinstance(price, str):
                        raise SchemaMismatch(f"mark history: bad row {[ts, price]!r}")
                    points[ts] = MarkPoint(
                        instrument_id=instrument_id,
                        timestamp=from_ms(ts),
                        mark_price=Decimal(price),
                    )
            if not more or not rows:
                break
            cursor = max(points) + _INTERVAL_MS[interval]
        else:
            raise VenueError(f"mark history: more than {_MAX_PAGES} pages")
        return tuple(points[ts] for ts in sorted(points))

    async def fetch_funding_history(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> tuple[FundingRate, ...]:
        async def call() -> list[PerpsFundingRate]:
            paginator = self._sdk.list_perps_funding_history(
                instrument_id=instrument_id, start=to_ms(start), end=to_ms(end)
            )
            return [f async for f in _bounded(paginator.iter_items())]

        raw = await self._read("funding history", call)
        with _translated("funding history"):
            rates = [map_funding(f, instrument_id) for f in raw]
        return tuple(sorted(rates, key=lambda f: f.timestamp))

    async def stream(
        self, instrument_ids: Sequence[int], channels: Sequence[StreamChannel]
    ) -> MarketStream:
        specs: list[PerpsSpec] = []
        for iid in instrument_ids:
            for channel in channels:
                match channel:
                    case StreamChannel.BBO:
                        specs.append(PerpsBboSpec(instrument_id=iid))
                    case StreamChannel.BOOK:
                        specs.append(PerpsBookSpec(instrument_id=iid))
                    case StreamChannel.TRADES:
                        specs.append(PerpsTradesSpec(instrument_id=iid))
                    case StreamChannel.TICKER:
                        specs.append(PerpsTickersSpec(instrument_id=iid))
        if not specs:
            raise ValueError("stream needs at least one instrument and channel")
        with _translated("subscribe"):
            handle = await self._sdk.subscribe(specs)
        return _SdkMarketStream(handle, frozenset(instrument_ids))


async def _bounded[T](items: AsyncIterator[T]) -> AsyncIterator[T]:
    """Guard against a paginator that never ends (a cursor bug would loop forever)."""
    limit = _MAX_PAGES * 1_000
    count = 0
    async for item in items:
        count += 1
        if count > limit:
            raise VenueError(f"pagination returned more than {limit} items")
        yield item


def _check_ids(operation: str, wanted: int, got: Iterable[int]) -> None:
    for iid in got:
        if iid != wanted:
            raise SchemaMismatch(f"{operation}: asked for {wanted}, got {iid}")


class _SdkMarketStream:
    """Adapts an SDK subscription handle to :class:`MarketStream`."""

    def __init__(self, handle: Any, instrument_ids: frozenset[int]) -> None:
        self._handle = handle
        self._instrument_ids = instrument_ids

    def __aiter__(self) -> AsyncIterator[MarketEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[MarketEvent]:
        with _translated("stream"):
            async for raw in self._handle:
                event = map_market_event(raw, utc_now())
                # Filter by instrument ourselves: the venue's instrument filters
                # have been unreliable (§14).
                if event is not None and event_instrument_id(event) in self._instrument_ids:
                    yield event

    async def close(self) -> None:
        await self._handle.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


__all__ = ["PolymarketMarketData", "parse_frame"]
