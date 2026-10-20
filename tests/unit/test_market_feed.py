import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from perps_agent.market import MarketFeed, Recorder, StatusRecord, read_recording
from perps_agent.venue import MarketDataSource, VenueUnavailable
from perps_agent.venue.fake import FakeExchange
from tests.unit.market_factories import (
    CONFIG,
    Clock,
    book,
    book_event,
    ticker_event,
    trade,
    trades_event,
)
from tests.unit.venue_factories import T0, instrument, ticker


class FlakyExchange(FakeExchange):
    """Counts subscriptions and can fail REST book reads."""

    def __init__(self) -> None:
        super().__init__([instrument()])
        self.subscriptions = 0
        self.book_failures = 0

    async def stream(self, instrument_ids, channels):  # type: ignore[no-untyped-def]
        self.subscriptions += 1
        return await super().stream(instrument_ids, channels)

    async def fetch_book(self, instrument_id, depth=100):  # type: ignore[no-untyped-def]
        if self.book_failures:
            self.book_failures -= 1
            raise VenueUnavailable("book: HTTP 503")
        return await super().fetch_book(instrument_id, depth)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def run_scenario(
    scenario: Callable[[FlakyExchange, MarketFeed, Clock], Awaitable[None]],
    recorder: Recorder | None = None,
) -> MarketFeed:
    exchange = FlakyExchange()
    exchange.books[19] = book(T0)
    exchange.tickers[19] = ticker()
    clock = Clock()
    source: MarketDataSource = exchange
    # A long check interval: the scenario drives the watchdog with feed.check().
    config = CONFIG.model_copy(update={"check_interval_ms": 5_000})
    feed = MarketFeed(source, [instrument()], config, recorder=recorder, clock=clock)

    async def main() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(feed.run(stop))
        await settle()
        try:
            await scenario(exchange, feed, clock)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(main())
    return feed


def publish_live(exchange: FakeExchange, clock: Clock) -> None:
    exchange.publish(book_event(clock.now), 19)
    exchange.publish(ticker_event(clock.now), 19)


def test_becomes_fresh_after_stream_and_rest_resnapshot() -> None:
    async def scenario(exchange: FlakyExchange, feed: MarketFeed, clock: Clock) -> None:
        assert feed.snapshot("NVDA-USD").stale
        await feed.check()  # stream not live yet: no re-snapshot
        assert feed.store.needs_resync(19)

        publish_live(exchange, clock)
        await settle()
        clock.advance(0.1)
        await feed.check()
        snap = feed.snapshot("NVDA-USD")
        assert not snap.stale, snap.stale_reasons

    run_scenario(scenario)


def test_gap_marks_stale_then_backfills_trades_before_fresh() -> None:
    async def scenario(exchange: FlakyExchange, feed: MarketFeed, clock: Clock) -> None:
        publish_live(exchange, clock)
        await settle()
        await feed.check()
        assert not feed.snapshot("NVDA-USD").stale

        clock.advance(2.5)  # silence longer than stale_after
        await feed.check()
        snap = feed.snapshot("NVDA-USD")
        assert snap.stale and "book: no stream update for 2.4s" in snap.stale_reasons

        # A trade printed during the gap; the stream comes back without it.
        missed = trade(7, clock.now - timedelta(seconds=1))
        exchange.trades[19] = [missed]
        clock.advance(0.5)
        publish_live(exchange, clock)
        await settle()
        assert feed.snapshot("NVDA-USD").stale_reasons == ("awaiting REST re-snapshot",)

        await feed.check()
        snap = feed.snapshot("NVDA-USD")
        assert not snap.stale
        assert [t.trade_id for t in snap.recent_trades] == [7]

        # Stream trades are applied too and deduplicated against the backfill.
        exchange.publish(trades_event(clock.now, missed, trade(8, clock.now)), 19)
        await settle()
        assert [t.trade_id for t in feed.snapshot("NVDA-USD").recent_trades] == [7, 8]

    run_scenario(scenario)


def test_failed_resnapshot_stays_stale_and_retries_later() -> None:
    async def scenario(exchange: FlakyExchange, feed: MarketFeed, clock: Clock) -> None:
        exchange.book_failures = 1
        publish_live(exchange, clock)
        await settle()
        await feed.check()
        assert feed.snapshot("NVDA-USD").stale

        clock.advance(1)  # before the retry delay: no new attempt
        publish_live(exchange, clock)
        await settle()
        await feed.check()
        assert feed.store.needs_resync(19)

        clock.advance(1.5)
        publish_live(exchange, clock)
        await settle()
        await feed.check()
        assert not feed.snapshot("NVDA-USD").stale

    run_scenario(scenario)


def test_a_silent_stream_is_closed_and_resubscribed() -> None:
    async def scenario(exchange: FlakyExchange, feed: MarketFeed, clock: Clock) -> None:
        assert exchange.subscriptions == 1
        clock.advance(16)  # past resubscribe_after with no frame at all
        await feed.check()
        # The stream loop pauses (1 s, real time) before subscribing again.
        for _ in range(30):
            await asyncio.sleep(0.1)
            if exchange.subscriptions == 2:
                break
        assert exchange.subscriptions == 2

        publish_live(exchange, clock)
        await settle()
        await feed.check()
        assert not feed.snapshot("NVDA-USD").stale

    run_scenario(scenario)


def test_recorder_sees_stream_rest_and_status(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path, {19: "NVDA-USD"})

    async def scenario(exchange: FlakyExchange, feed: MarketFeed, clock: Clock) -> None:
        publish_live(exchange, clock)
        await settle()
        await feed.check()
        clock.advance(3)
        await feed.check()

    run_scenario(scenario, recorder)
    recorder.close()
    records = list(read_recording(tmp_path / "2026-10-20" / "NVDA-USD.jsonl"))
    assert [r.type for r in records] == [
        "status",  # subscribed
        "stream",  # book
        "stream",  # ticker
        "rest",
        "status",  # fresh
        "status",  # stale
        "status",  # stopped
    ]
    statuses = [r.status for r in records if isinstance(r, StatusRecord)]
    assert statuses == ["subscribed", "fresh", "stale", "stopped"]
