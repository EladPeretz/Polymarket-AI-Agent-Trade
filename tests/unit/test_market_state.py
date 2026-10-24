from datetime import timedelta
from decimal import Decimal

import pytest

from perps_agent.market import MarketStateStore
from perps_agent.venue import StreamChannel, UnknownInstrument
from tests.unit.market_factories import (
    bbo_event,
    book,
    book_event,
    ticker_event,
    trade,
    trades_event,
)
from tests.unit.venue_factories import T0, instrument, ticker


def store() -> MarketStateStore:
    return MarketStateStore(
        [instrument()],
        stale_after=timedelta(seconds=2),
        snapshot_depth=1,
        recent_trades=timedelta(minutes=5),
    )


def live(s: MarketStateStore, seconds: float = 0) -> None:
    """Deliver a book and a ticker frame at ``T0 + seconds``."""
    at = T0 + timedelta(seconds=seconds)
    s.apply_stream(book_event(at))
    s.apply_stream(ticker_event(at))


def resync(s: MarketStateStore, seconds: float = 0, trades: tuple = ()) -> None:
    at = T0 + timedelta(seconds=seconds)
    s.apply_rest(19, book=book(at), ticker=ticker(), trades=trades, now=at)


def test_starts_stale_until_stream_is_live_and_resnapshotted() -> None:
    s = store()
    snap = s.snapshot("NVDA-USD", T0)
    assert snap.stale
    assert snap.stale_reasons == (
        "book: no stream update yet",
        "ticker: no stream update yet",
        "awaiting REST re-snapshot",
    )

    live(s)
    assert s.snapshot("NVDA-USD", T0 + timedelta(milliseconds=100)).stale_reasons == (
        "awaiting REST re-snapshot",
    )

    resync(s)
    snap = s.snapshot("NVDA-USD", T0 + timedelta(milliseconds=100))
    assert not snap.stale and snap.stale_reasons == ()
    assert snap.best_bid is not None and snap.best_bid.price == Decimal("199.9")
    assert len(snap.bids) == len(snap.asks) == 1  # snapshot_depth
    assert snap.spread_bp == Decimal("0.2") / Decimal(200) * 10_000
    assert snap.mark_price == Decimal(200)
    assert snap.channel_age_ms[StreamChannel.BOOK] == 20
    assert snap.channel_age_ms[StreamChannel.BBO] is None


def test_a_quiet_required_channel_is_stale_by_time() -> None:
    s = store()
    live(s)
    resync(s)
    s.apply_stream(book_event(T0 + timedelta(seconds=3)))  # only the book keeps coming
    snap = s.snapshot("NVDA-USD", T0 + timedelta(seconds=3))
    assert snap.stale
    assert snap.stale_reasons == ("ticker: no stream update for 2.9s",)


def test_frames_with_a_frozen_venue_timestamp_are_stale() -> None:
    s = store()
    live(s)
    resync(s)
    frozen = book_event(T0).model_copy(update={"received_at": T0 + timedelta(seconds=3)})
    s.apply_stream(frozen)
    s.apply_stream(ticker_event(T0 + timedelta(seconds=3)))
    assert s.stream_problems(19, T0 + timedelta(seconds=3)) == ["book: venue timestamp 3.0s old"]


def test_bbo_and_trades_are_not_required() -> None:
    s = store()
    live(s, 10)
    resync(s, 10)
    s.apply_stream(bbo_event(T0))
    assert not s.snapshot("NVDA-USD", T0 + timedelta(seconds=10)).stale


def test_sequence_jumps_do_not_matter_and_older_data_never_replaces_newer() -> None:
    s = store()
    s.apply_stream(book_event(T0 + timedelta(seconds=1), sequence=900, bid="199.5"))
    s.apply_stream(book_event(T0, sequence=5_000, bid="150"))  # older, though higher sq
    resync(s, 0.5)  # REST book older than the stream's
    snap = s.snapshot("NVDA-USD", T0 + timedelta(seconds=1))
    assert snap.best_bid is not None and snap.best_bid.price == Decimal("199.5")
    assert snap.book_time == T0 + timedelta(seconds=1)


def test_trades_are_deduplicated_ordered_and_windowed() -> None:
    s = store()
    old = trade(1, T0 - timedelta(minutes=10))
    a = trade(2, T0)
    b = trade(3, T0 + timedelta(seconds=1))
    s.apply_stream(trades_event(T0 + timedelta(seconds=1), b, a))
    resync(s, 1, trades=(old, a, b))  # REST backfill overlaps the stream
    snap = s.snapshot("NVDA-USD", T0 + timedelta(seconds=1))
    assert [t.trade_id for t in snap.recent_trades] == [2, 3]
    assert s.last_trade_time(19) == b.timestamp

    later = T0 + timedelta(minutes=5, seconds=0.5)
    assert [t.trade_id for t in s.snapshot("NVDA-USD", later).recent_trades] == [3]


def test_other_instruments_are_ignored() -> None:
    s = store()
    other = book_event(T0).model_copy(update={"book": book(T0, instrument_id=1)})
    assert s.apply_stream(other) is False
    assert s.apply_stream(trades_event(T0)) is False  # empty trades frame
    with pytest.raises(UnknownInstrument):
        s.snapshot("BTC-USD", T0)


def test_a_gap_requires_a_new_resnapshot() -> None:
    s = store()
    live(s)
    resync(s)
    s.mark_gap(19)
    live(s, 0.1)
    assert s.snapshot("NVDA-USD", T0 + timedelta(seconds=0.2)).stale_reasons == (
        "awaiting REST re-snapshot",
    )
