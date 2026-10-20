"""Market state store and the read-only ``MarketSnapshot`` (overview §3.2).

The store holds the latest book, BBO, ticker and recent trades per instrument.
It is fed by :class:`perps_agent.market.feed.MarketFeed` and has no I/O of its
own, so tests drive it with explicit times.

Staleness is judged by time, never by sequence: ``sq`` is shared across
channels and jumps by irregular steps. A required channel (book, ticker) is
stale when the stream has not delivered it, or its venue timestamp has not
advanced, for ``stale_after``. After any gap, and at startup, the instrument
stays stale until the feed has re-snapshotted it via REST.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from perps_agent.venue import (
    Bbo,
    BboEvent,
    BookEvent,
    BookLevel,
    Instrument,
    MarketEvent,
    OrderBook,
    StreamChannel,
    Ticker,
    TickerEvent,
    Trade,
    TradesEvent,
    UnknownInstrument,
)
from perps_agent.venue.models import UtcDatetime

# Channels that heartbeat every 100 ms. BBO and trades only arrive on change.
REQUIRED_CHANNELS = (StreamChannel.BOOK, StreamChannel.TICKER)
# Upper bound on buffered trades per instrument, whatever the time window.
MAX_RECENT_TRADES = 5_000


class MarketSnapshot(BaseModel):
    """Read-only market state for one instrument at ``taken_at``.

    ``stale`` is the only freshness flag consumers need: when it is set the risk
    engine blocks new entries, and ``stale_reasons`` says why.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    instrument_id: int
    taken_at: UtcDatetime
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    book_time: UtcDatetime | None
    bbo: Bbo | None
    ticker: Ticker | None
    recent_trades: tuple[Trade, ...]
    # Milliseconds since the stream last delivered each channel (None: never).
    channel_age_ms: dict[StreamChannel, Annotated[int, Field(ge=0)] | None]
    stale: bool
    stale_reasons: tuple[str, ...]

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid.price + self.best_ask.price) / 2

    @property
    def spread_bp(self) -> Decimal | None:
        mid = self.mid
        if mid is None or self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_ask.price - self.best_bid.price) / mid * 10_000

    @property
    def mark_price(self) -> Decimal | None:
        return self.ticker.mark_price if self.ticker else None


@dataclass(slots=True)
class _InstrumentState:
    instrument: Instrument
    book: OrderBook | None = None
    bbo: Bbo | None = None
    ticker: Ticker | None = None
    trades: list[Trade] = field(default_factory=list)
    trade_ids: set[int] = field(default_factory=set)
    # When the stream last delivered each channel (local receipt time).
    received: dict[StreamChannel, datetime] = field(default_factory=dict)
    # Startup counts as a gap: nothing is fresh until the first REST re-snapshot.
    needs_resync: bool = True


class MarketStateStore:
    def __init__(
        self,
        instruments: Sequence[Instrument],
        *,
        stale_after: timedelta,
        snapshot_depth: int,
        recent_trades: timedelta,
    ) -> None:
        self._states = {i.instrument_id: _InstrumentState(i) for i in instruments}
        self._ids = {i.symbol: i.instrument_id for i in instruments}
        self._stale_after = stale_after
        self._depth = snapshot_depth
        self._recent_trades = recent_trades

    @property
    def instrument_ids(self) -> tuple[int, ...]:
        return tuple(self._states)

    def symbol(self, instrument_id: int) -> str:
        return self._state(instrument_id).instrument.symbol

    # --- Updates ---

    def apply_stream(self, event: MarketEvent) -> bool:
        """Apply a stream event. Returns False for an instrument this store does not track."""
        iid = instrument_of(event)
        state = self._states.get(iid) if iid is not None else None
        if state is None:
            return False
        state.received[event.kind] = max(
            event.received_at, state.received.get(event.kind, event.received_at)
        )
        match event:
            case BboEvent():
                if state.bbo is None or event.bbo.timestamp >= state.bbo.timestamp:
                    state.bbo = event.bbo
            case BookEvent():
                self._set_book(state, event.book)
            case TickerEvent():
                self._set_ticker(state, event.ticker)
            case TradesEvent():
                self._add_trades(state, event.trades, event.received_at)
        return True

    def apply_rest(
        self,
        instrument_id: int,
        *,
        book: OrderBook,
        ticker: Ticker,
        trades: Iterable[Trade],
        now: datetime,
    ) -> None:
        """Apply a REST re-snapshot and clear the resync flag.

        Older data never replaces newer: the stream may have moved on while the
        REST calls were in flight.
        """
        state = self._state(instrument_id)
        self._set_book(state, book)
        self._set_ticker(state, ticker)
        self._add_trades(state, trades, now)
        state.needs_resync = False

    def mark_gap(self, instrument_id: int) -> None:
        """Require a REST re-snapshot before the instrument is fresh again."""
        self._state(instrument_id).needs_resync = True

    # --- Queries ---

    def needs_resync(self, instrument_id: int) -> bool:
        return self._state(instrument_id).needs_resync

    def last_trade_time(self, instrument_id: int) -> datetime | None:
        trades = self._state(instrument_id).trades
        return trades[-1].timestamp if trades else None

    def stream_problems(self, instrument_id: int, now: datetime) -> list[str]:
        """Why the stream's required channels are not live at ``now`` (empty: live)."""
        state = self._state(instrument_id)
        problems = []
        for channel in REQUIRED_CHANNELS:
            received = state.received.get(channel)
            if received is None:
                problems.append(f"{channel}: no stream update yet")
                continue
            age = now - received
            if age > self._stale_after:
                problems.append(f"{channel}: no stream update for {_seconds(age)}")
                continue
            venue_time = _venue_time(state, channel)
            if venue_time is not None and now - venue_time > self._stale_after:
                problems.append(f"{channel}: venue timestamp {_seconds(now - venue_time)} old")
        return problems

    def snapshot(self, symbol: str, now: datetime) -> MarketSnapshot:
        iid = self._ids.get(symbol)
        if iid is None:
            raise UnknownInstrument(f"market state does not track {symbol!r}")
        state = self._states[iid]
        reasons = self.stream_problems(iid, now)
        if state.needs_resync:
            reasons.append("awaiting REST re-snapshot")
        cutoff = now - self._recent_trades
        book = state.book
        return MarketSnapshot(
            symbol=symbol,
            instrument_id=iid,
            taken_at=now,
            bids=book.bids[: self._depth] if book else (),
            asks=book.asks[: self._depth] if book else (),
            book_time=book.timestamp if book else None,
            bbo=state.bbo,
            ticker=state.ticker,
            recent_trades=tuple(t for t in state.trades if t.timestamp >= cutoff),
            channel_age_ms={
                channel: (
                    max(0, (now - state.received[channel]) // timedelta(milliseconds=1))
                    if channel in state.received
                    else None
                )
                for channel in StreamChannel
            },
            stale=bool(reasons),
            stale_reasons=tuple(reasons),
        )

    # --- Internals ---

    def _state(self, instrument_id: int) -> _InstrumentState:
        try:
            return self._states[instrument_id]
        except KeyError:
            raise UnknownInstrument(f"market state does not track {instrument_id}") from None

    @staticmethod
    def _set_book(state: _InstrumentState, book: OrderBook) -> None:
        if state.book is None or book.timestamp >= state.book.timestamp:
            state.book = book

    @staticmethod
    def _set_ticker(state: _InstrumentState, ticker: Ticker) -> None:
        if state.ticker is None or ticker.timestamp >= state.ticker.timestamp:
            state.ticker = ticker

    def _add_trades(self, state: _InstrumentState, trades: Iterable[Trade], now: datetime) -> None:
        new = [t for t in trades if t.trade_id not in state.trade_ids]
        if new:
            state.trades.extend(new)
            state.trades.sort(key=lambda t: (t.timestamp, t.trade_id))
        cutoff = now - self._recent_trades
        keep = [t for t in state.trades if t.timestamp >= cutoff][-MAX_RECENT_TRADES:]
        if len(keep) != len(state.trades):
            state.trades = keep
            state.trade_ids = {t.trade_id for t in keep}
        else:
            state.trade_ids.update(t.trade_id for t in new)


def instrument_of(event: MarketEvent) -> int | None:
    """The instrument an event is for; None for an empty trades frame."""
    match event:
        case BboEvent():
            return event.bbo.instrument_id
        case BookEvent():
            return event.book.instrument_id
        case TickerEvent():
            return event.ticker.instrument_id
        case TradesEvent():
            return event.trades[0].instrument_id if event.trades else None


def _venue_time(state: _InstrumentState, channel: StreamChannel) -> datetime | None:
    if channel is StreamChannel.BOOK and state.book is not None:
        return state.book.timestamp
    if channel is StreamChannel.TICKER and state.ticker is not None:
        return state.ticker.timestamp
    return None


def _seconds(delta: timedelta) -> str:
    return f"{delta.total_seconds():.1f}s"
