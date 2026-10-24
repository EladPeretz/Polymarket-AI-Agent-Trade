"""Contract tests for WebSocket frame parsing and the stream adapter.

``frames.jsonl`` is a live recording; ``synthetic.jsonl`` fills its gaps (see
fixtures/README.md).
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


def _first(channel: str, name: str = "frames.jsonl") -> dict[str, Any]:
    return next(f for f in load_frames(name) if f.get("ch") == channel)


def test_ack_frames_yield_nothing() -> None:
    ack = next(f for f in load_frames() if "ch" not in f)
    assert parse_frame(ack, NOW) == ()


def test_every_recorded_update_parses() -> None:
    updates = [f for f in load_frames() if "ch" in f]
    assert {f["ch"].split("::")[0] for f in updates} >= {"bbo", "book", "tickers"}
    for frame in updates:
        assert len(parse_frame(frame, NOW)) == 1, frame["ch"]


def test_bbo_frame() -> None:
    raw = _first("bbo::19")
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, BboEvent)
    assert event.channel == "bbo::19" and event.sequence == raw["sq"]
    assert event.received_at == NOW
    bbo = event.bbo
    assert bbo.bid_price == Decimal(raw["data"]["bp"])
    assert bbo.ask_quantity == Decimal(raw["data"]["aq"])
    assert bbo.timestamp == from_ms(raw["ts"])
    assert bbo.spread_bp > 0


def test_book_frame_is_a_snapshot_with_frame_sequence() -> None:
    raw = _first("book::19")
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, BookEvent)
    assert event.book.instrument_id == 19
    assert event.book.sequence == raw["sq"]
    assert len(event.book.bids) == len(raw["data"]["b"])
    assert event.book.asks[0].price == Decimal(raw["data"]["a"][0][0])
    assert event.book.best_bid is not None and event.book.best_ask is not None
    assert event.book.best_bid.price < event.book.best_ask.price


def test_ticker_frame() -> None:
    raw = _first("tickers::19")
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, TickerEvent)
    assert event.ticker.instrument_id == 19
    assert event.ticker.mark_price == Decimal(raw["data"]["mark"])
    assert event.ticker.next_funding == from_ms(raw["data"]["nxf"])
    assert event.ticker.timestamp == from_ms(raw["ts"])


def test_trades_frame() -> None:
    raw = _first("trades::19", "synthetic.jsonl")
    (event,) = parse_frame(raw, NOW)
    assert isinstance(event, TradesEvent)
    assert [t.aggressor for t in event.trades] == [Side.BUY, Side.SELL]
    assert event.trades[1].quantity == Decimal("1.25")
    assert event.trades[0].timestamp == from_ms(raw["data"][0]["ts"])


def test_malformed_frame_is_dropped() -> None:
    malformed = load_frames("synthetic.jsonl")[-1]
    assert "ap" not in malformed["data"]
    assert parse_frame(malformed, NOW) == ()


class _Handle:
    """Stands in for an SDK subscription handle, replaying frames."""

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
    frames = load_frames() + load_frames("synthetic.jsonl")
    handle = _Handle(frames)

    async def run():
        async with _SdkMarketStream(handle, frozenset({19})) as stream:
            return [event async for event in stream]

    events = asyncio.run(run())
    assert handle.closed
    updates = [f for f in frames if "ch" in f and f["ch"] != "tickers::1"]
    assert len(events) == len(updates) - 1  # minus the malformed frame
    assert {type(e) for e in events} == {BboEvent, BookEvent, TickerEvent, TradesEvent}
    assert all(e.received_at.tzinfo is UTC for e in events)
    assert not any(isinstance(e, TickerEvent) and e.ticker.instrument_id == 1 for e in events)
