"""Market data feed: keeps the state store current from the venue stream (overview §3.2).

Two tasks run until ``stop`` is set:

* the **stream loop** subscribes to book, ticker, BBO and trades for every
  instrument and applies each event to the store (and the recorder). If the
  subscription fails or ends, it subscribes again after a short pause.
* the **watchdog** runs every ``check_interval``. The SDK reconnects silently
  and ``sq`` cannot reveal a gap, so it judges by time:

  - a required channel quiet for ``stale_after`` marks the instrument stale and
    flags it for a REST re-snapshot;
  - once the stream is live again it re-snapshots via REST (book, ticker and
    the trades missed during the gap); only then is the instrument fresh;
  - no frame at all for ``resubscribe_after`` closes the subscription so the
    stream loop opens a new one.

It only reads public market data: no credentials, no orders.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from perps_agent.clock import utc_now
from perps_agent.config.models import MarketDataConfig
from perps_agent.market.recorder import FeedStatus, Recorder
from perps_agent.market.state import MarketSnapshot, MarketStateStore, instrument_of
from perps_agent.venue import (
    Instrument,
    MarketDataSource,
    MarketStream,
    StreamChannel,
    VenueError,
)
from perps_agent.venue.interfaces import BookDepth

log = logging.getLogger(__name__)

CHANNELS = (StreamChannel.BOOK, StreamChannel.TICKER, StreamChannel.BBO, StreamChannel.TRADES)
_RESUBSCRIBE_PAUSE = timedelta(seconds=1)
_RESYNC_RETRY = timedelta(seconds=2)


class MarketFeed:
    def __init__(
        self,
        source: MarketDataSource,
        instruments: Sequence[Instrument],
        config: MarketDataConfig,
        *,
        recorder: Recorder | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not instruments:
            raise ValueError("the feed needs at least one instrument")
        self._source = source
        self._config = config
        self._recorder = recorder
        self._clock = clock
        self.store = MarketStateStore(
            instruments,
            stale_after=timedelta(milliseconds=config.stale_after_ms),
            snapshot_depth=config.snapshot_depth,
            recent_trades=timedelta(seconds=config.recent_trades_seconds),
        )
        self._resubscribe_after = timedelta(milliseconds=config.resubscribe_after_ms)
        self._stream: MarketStream | None = None
        self._last_frame_at: datetime | None = None
        # Instruments currently stale, so each gap is reported once. Startup is
        # not reported: every instrument starts stale until its first re-snapshot.
        self._stale: set[int] = set(self.store.instrument_ids)
        self._next_resync: dict[int, datetime] = {}

    def snapshot(self, symbol: str) -> MarketSnapshot:
        return self.store.snapshot(symbol, self._clock())

    async def run(self, stop: asyncio.Event) -> None:
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._stream_loop(stop))
                tasks.create_task(self._watchdog_loop(stop))
        finally:
            now = self._clock()
            for iid in self.store.instrument_ids:
                self._status(iid, "stopped", now)

    # --- Stream ---

    async def _stream_loop(self, stop: asyncio.Event) -> None:
        ids = self.store.instrument_ids
        while not stop.is_set():
            try:
                stream = await self._source.stream(ids, CHANNELS)
            except VenueError as error:
                log.warning("market subscribe failed", extra={"error": str(error)})
                await _wait(stop, _RESUBSCRIBE_PAUSE)
                continue
            self._stream = stream
            self._last_frame_at = self._clock()
            for iid in ids:
                self._status(iid, "subscribed", self._last_frame_at)
            try:
                async with stream:
                    async for event in stream:
                        self._last_frame_at = event.received_at
                        target = instrument_of(event)
                        if self.store.apply_stream(event) and self._recorder is not None:
                            assert target is not None  # applied events always have one
                            self._recorder.stream(event, target)
            except VenueError as error:
                log.warning("market stream failed", extra={"error": str(error)})
            finally:
                self._stream = None
            if not stop.is_set():
                await _wait(stop, _RESUBSCRIBE_PAUSE)

    async def _close_stream(self) -> None:
        stream = self._stream
        if stream is not None:
            try:
                await stream.close()
            except VenueError as error:
                log.warning("closing market stream failed", extra={"error": str(error)})

    # --- Watchdog ---

    async def _watchdog_loop(self, stop: asyncio.Event) -> None:
        interval = timedelta(milliseconds=self._config.check_interval_ms)
        while not stop.is_set():
            await self.check()
            await _wait(stop, interval)
        await self._close_stream()

    async def check(self) -> None:
        """One watchdog pass: detect gaps, re-snapshot recovered instruments, resubscribe."""
        now = self._clock()
        for iid in self.store.instrument_ids:
            problems = self.store.stream_problems(iid, now)
            if problems and iid not in self._stale:
                self._stale.add(iid)
                self.store.mark_gap(iid)
                detail = "; ".join(problems)
                log.warning(
                    "market data stale",
                    extra={"symbol": self.store.symbol(iid), "problems": problems},
                )
                self._status(iid, "stale", now, detail)
            if (
                not problems
                and self.store.needs_resync(iid)
                and now >= self._next_resync.get(iid, now)
            ):
                await self._resync(iid)

        if (
            self._stream is not None
            and self._last_frame_at is not None
            and now - self._last_frame_at > self._resubscribe_after
        ):
            log.warning(
                "market stream silent, resubscribing",
                extra={"silent_s": (now - self._last_frame_at).total_seconds()},
            )
            for iid in self.store.instrument_ids:
                self._status(iid, "resubscribing", now)
            self._last_frame_at = now
            await self._close_stream()

    async def _resync(self, instrument_id: int) -> None:
        start = self._clock()
        window_start = start - timedelta(seconds=self._config.recent_trades_seconds)
        since = max(self.store.last_trade_time(instrument_id) or window_start, window_start)
        depth: BookDepth = 10 if self._config.snapshot_depth <= 10 else 100
        try:
            book = await self._source.fetch_book(instrument_id, depth=depth)
            ticker = await self._source.fetch_ticker(instrument_id)
            trades = await self._source.fetch_trades(instrument_id, since, start)
        except VenueError as error:
            self._next_resync[instrument_id] = start + _RESYNC_RETRY
            log.warning(
                "market re-snapshot failed",
                extra={"symbol": self.store.symbol(instrument_id), "error": str(error)},
            )
            return
        now = self._clock()
        self.store.apply_rest(instrument_id, book=book, ticker=ticker, trades=trades, now=now)
        self._next_resync.pop(instrument_id, None)
        if self._recorder is not None:
            self._recorder.rest(instrument_id, book=book, ticker=ticker, trades=trades, at=now)
        self._stale.discard(instrument_id)
        log.info("market data fresh", extra={"symbol": self.store.symbol(instrument_id)})
        self._status(instrument_id, "fresh", now)

    def _status(
        self, instrument_id: int, status: FeedStatus, at: datetime, detail: str = ""
    ) -> None:
        if self._recorder is not None:
            self._recorder.status(instrument_id, status, at, detail)


async def _wait(stop: asyncio.Event, delay: timedelta) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay.total_seconds())
    except TimeoutError:
        pass
