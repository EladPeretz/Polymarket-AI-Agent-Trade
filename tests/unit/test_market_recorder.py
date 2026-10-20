from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from perps_agent.market import (
    Recorder,
    RestRecord,
    StatusRecord,
    StreamRecord,
    read_recording,
    recording_path,
)
from tests.unit.market_factories import book, book_event, ticker_event, trade, trades_event
from tests.unit.venue_factories import T0, ticker


def test_round_trip(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path, {19: "NVDA-USD"})
    events = [book_event(T0), ticker_event(T0), trades_event(T0, trade(1, T0))]
    for event in events:
        recorder.stream(event, 19)
    recorder.rest(19, book=book(T0), ticker=ticker(), trades=(trade(1, T0),), at=T0)
    recorder.status(19, "stale", T0, "book: no stream update for 2.1s")
    recorder.close()

    records = list(read_recording(recording_path(tmp_path, "NVDA-USD", T0)))
    assert [r.event for r in records if isinstance(r, StreamRecord)] == events
    rest = next(r for r in records if isinstance(r, RestRecord))
    assert rest.book == book(T0) and rest.trades == (trade(1, T0),)
    status = next(r for r in records if isinstance(r, StatusRecord))
    assert status.status == "stale" and status.detail.startswith("book:")


def test_repeated_book_and_ticker_frames_are_written_once(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path, {19: "NVDA-USD"})
    for ms in range(0, 500, 100):  # same content, new timestamps
        at = T0 + timedelta(milliseconds=ms)
        recorder.stream(book_event(at), 19)
        recorder.stream(ticker_event(at), 19)
    recorder.stream(book_event(T0 + timedelta(seconds=1), bid="199.8"), 19)
    recorder.stream(ticker_event(T0 + timedelta(seconds=1), mark="201"), 19)
    # After a reported gap the next frames are written even if unchanged.
    recorder.status(19, "stale", T0 + timedelta(seconds=4))
    recorder.stream(ticker_event(T0 + timedelta(seconds=5), mark="201"), 19)
    recorder.close()

    records = list(read_recording(recording_path(tmp_path, "NVDA-USD", T0)))
    kinds = [r.event.kind.value if isinstance(r, StreamRecord) else r.type for r in records]
    assert kinds == ["book", "ticker", "book", "ticker", "status", "ticker"]


def test_one_file_per_utc_day(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path, {19: "NVDA-USD"})
    late = T0.replace(hour=23, minute=59, second=59)
    recorder.status(19, "subscribed", late)
    recorder.status(19, "stopped", late + timedelta(seconds=2))
    recorder.close()
    assert (tmp_path / "2026-10-20" / "NVDA-USD.jsonl").exists()
    assert (tmp_path / "2026-10-21" / "NVDA-USD.jsonl").exists()


def test_torn_last_line_is_skipped_but_corruption_elsewhere_raises(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path, {19: "NVDA-USD"})
    recorder.status(19, "subscribed", T0)
    recorder.status(19, "fresh", T0)
    recorder.close()
    path = recording_path(tmp_path, "NVDA-USD", T0)
    text = path.read_text()
    path.write_text(text + '{"at": "2026-10-20T13:30:00Z", "instr')
    assert len(list(read_recording(path))) == 2

    path.write_text("not json\n" + text)
    with pytest.raises(ValidationError):
        list(read_recording(path))


def test_write_failure_disables_the_recorder_without_raising(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    recorder = Recorder(blocker, {19: "NVDA-USD"})  # a file where a directory should be
    recorder.stream(book_event(T0), 19)
    assert recorder.failed
    recorder.status(19, "fresh", T0)  # ignored, no exception
