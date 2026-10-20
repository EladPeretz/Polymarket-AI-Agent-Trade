"""Live read-only test of the market feed and recorder. Skipped unless asked for:

    PERPS_AGENT_LIVE_TESTS=1 pytest tests/live

Public endpoints only. Needs a network that allows WebSocket connections.
"""

import asyncio
import os
from pathlib import Path

import pytest

from perps_agent.config.models import MarketDataConfig, VenueConfig
from perps_agent.market import MarketFeed, Recorder, StatusRecord, read_recording
from perps_agent.venue import InstrumentRegistry, create_market_data

pytestmark = pytest.mark.skipif(
    os.environ.get("PERPS_AGENT_LIVE_TESTS") != "1",
    reason="live venue tests run only with PERPS_AGENT_LIVE_TESTS=1",
)

SYMBOL = os.environ.get("PERPS_AGENT_LIVE_SYMBOL", "NVDA-USD")


def test_feed_becomes_fresh_and_records(tmp_path: Path) -> None:
    async def run():
        source = create_market_data(VenueConfig())
        try:
            registry = InstrumentRegistry(source)
            await registry.refresh()
            instrument = registry.by_symbol(SYMBOL)
            recorder = Recorder(tmp_path, {instrument.instrument_id: SYMBOL})
            feed = MarketFeed(source, [instrument], MarketDataConfig(), recorder=recorder)
            stop = asyncio.Event()
            task = asyncio.create_task(feed.run(stop))
            try:
                async with asyncio.timeout(30):
                    while feed.snapshot(SYMBOL).stale:
                        await asyncio.sleep(0.2)
                await asyncio.sleep(1)
                return feed.snapshot(SYMBOL)
            finally:
                stop.set()
                await task
                recorder.close()
        finally:
            await source.aclose()

    snapshot = asyncio.run(run())
    assert not snapshot.stale
    assert snapshot.best_bid and snapshot.best_ask
    assert snapshot.best_bid.price < snapshot.best_ask.price
    assert snapshot.ticker is not None

    (path,) = tmp_path.rglob("*.jsonl")
    records = list(read_recording(path))
    statuses = [r.status for r in records if isinstance(r, StatusRecord)]
    assert statuses[0] == "subscribed" and "fresh" in statuses and statuses[-1] == "stopped"
    assert any(r.type == "rest" for r in records)
    assert any(r.type == "stream" for r in records)
