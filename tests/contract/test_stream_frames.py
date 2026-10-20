"""Contract tests for WebSocket frame parsing and the stream adapter.

The frames are written from the AsyncAPI examples (see fixtures/README.md).
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from polymarket.models.perps.events import parse_perps_market_events

from perps_agent.clock import from_ms
from perps_agent.venue import BboEvent, BookEvent, Side, TickerEvent, TradesEvent
from perps_agent.venue.polymarket_source import _SdkMarketStream, parse_frame
from tests.contract.conftest import load_frames

NOW = datetime(2026, 10, 20, 10, 54, tzinfo=UTC)


def _parse_all() -> list[Any]:
    return [event for frame in load_frames() for event in parse_frame(frame, NOW)]


def test_ack_frames_yield_nothing() -> None:
    assert parse_frame(load_frames()[0], NOW) == ()


def test_bbo_frame() -> None:
    raw = load_frames()[1]
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, BboEvent)
    assert event.channel == "bbo::19" and event.sequence == raw["sq"]
    assert event.received_at == NOW
    bbo = event.bbo
    assert (bbo.bid_price, bbo.ask_price) == (Decimal("216.27"), Decimal("216.29"))
    assert bbo.timestamp == from_ms(raw["ts"])
    assert bbo.spread_bp > 0


def test_book_frame_is_a_snapshot_with_channel_sequence() -> None:
    raw = load_frames()[2]
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, BookEvent)
    assert event.book.instrument_id == 19
    assert event.book.sequence == raw["sq"]
    assert len(event.book.bids) == len(raw["data"]["b"])
    assert event.book.asks[0].price == Decimal("216.29")


def test_trades_frame() -> None:
    raw = load_frames()[3]
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, TradesEvent)
    assert [t.aggressor for t in event.trades] == [Side.BUY, Side.SELL]
    assert event.trades[1].quantity == Decimal("1.25")
    assert event.trades[0].timestamp == from_ms(raw["data"][0]["ts"])


def test_ticker_frame() -> None:
    raw = load_frames()[4]
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, TickerEvent)
    assert event.ticker.instrument_id == 19
    assert event.ticker.mark_price == Decimal("216.28")
    assert event.ticker.next_funding == from_ms(raw["data"]["nxf"])
    assert event.ticker.timestamp == from_ms(raw["ts"])


def test_malformed_frame_is_dropped() -> None:
    assert parse_frame(load_frames()[-1], NOW) == ()


class _Handle:
    """Stands in for an SDK subscription handle, replaying recorded frames."""

    def __init__(self, frames: list[Any]) -> None:
        self._events = [e for f in frames for e in parse_perps_market_events(f)[0]]
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            yield event

    async def close(self) -> None:
        self.closed = True


def test_stream_keeps_only_subscribed_instruments() -> None:
    handle = _Handle(load_frames())

    async def run():
        async with _SdkMarketStream(handle, frozenset({19})) as stream:
            return [event async for event in stream]

    events = asyncio.run(run())
    assert handle.closed
    assert [type(e) for e in events] == [BboEvent, BookEvent, TradesEvent, TickerEvent]
    assert all(e.received_at.tzinfo is UTC for e in events)
    assert len(_parse_all()) == 5  # the SP500 ticker is parsed, then filtered by the stream
