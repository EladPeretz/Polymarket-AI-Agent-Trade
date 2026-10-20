"""Live read-only smoke tests against the real venue. Skipped unless asked for:

    PERPS_AGENT_LIVE_TESTS=1 pytest tests/live

Public endpoints only: no credentials, nothing signed, no orders. The stream test
needs a network that allows WebSocket connections.
"""

import asyncio
import os
from datetime import timedelta

import pytest

from perps_agent.clock import utc_now
from perps_agent.config.models import VenueConfig
from perps_agent.venue import (
    BboEvent,
    InstrumentRegistry,
    StreamChannel,
    TickerEvent,
    create_market_data,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("PERPS_AGENT_LIVE_TESTS") != "1",
    reason="live venue tests run only with PERPS_AGENT_LIVE_TESTS=1",
)

SYMBOL = os.environ.get("PERPS_AGENT_LIVE_SYMBOL", "NVDA-USD")


def test_rest_reads_without_credentials() -> None:
    async def run():
        source = create_market_data(VenueConfig())
        try:
            registry = InstrumentRegistry(source)
            await registry.refresh()
            instrument = registry.by_symbol(SYMBOL)
            ticker = await source.fetch_ticker(instrument.instrument_id)
            book = await source.fetch_book(instrument.instrument_id, depth=10)
            now = utc_now()
            funding = await source.fetch_funding_history(
                instrument.instrument_id, now - timedelta(hours=6), now
            )
            return instrument, ticker, book, funding
        finally:
            await source.aclose()

    instrument, ticker, book, funding = asyncio.run(run())
    assert ticker.instrument_id == instrument.instrument_id
    assert book.best_bid and book.best_ask and book.best_bid.price < book.best_ask.price
    # Mark should sit near the book: a wrong-instrument ticker would be far off.
    assert abs(ticker.mark_price / book.best_bid.price - 1) < instrument.price_band
    assert funding


def test_stream_bbo_and_ticker() -> None:
    async def run():
        source = create_market_data(VenueConfig())
        try:
            instrument = next(i for i in await source.fetch_instruments() if i.symbol == SYMBOL)
            seen: set[type] = set()
            stream = await source.stream(
                [instrument.instrument_id], [StreamChannel.BBO, StreamChannel.TICKER]
            )
            async with stream, asyncio.timeout(30):
                async for event in stream:
                    seen.add(type(event))
                    if {BboEvent, TickerEvent} <= seen:
                        break
            return seen
        finally:
            await source.aclose()

    assert {BboEvent, TickerEvent} <= asyncio.run(run())
