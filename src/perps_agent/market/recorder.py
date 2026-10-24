"""Market data recorder: an append-only JSONL log of what the feed saw.

One file per UTC day and symbol: ``<dir>/<YYYY-MM-DD>/<SYMBOL>.jsonl``. Each line
is one :data:`Record`: a stream event, a REST re-snapshot, or a feed status
change (stale, fresh, resubscribed), so a replay shows the gaps as well as the
data. Book and ticker frames arrive every 100 ms but mostly repeat; a frame is
only written when its content differs from the last one written.

Files are line-buffered, so a crash loses at most a partial line, which
:func:`read_recording` skips. A write failure disables the recorder and is
logged; it never stops the feed. Recordings are local and git-ignored
(ADR-0006); the simulator and backtests replay them.
"""

import logging
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from perps_agent.venue import (
    BboEvent,
    BookEvent,
    MarketEvent,
    OrderBook,
    Ticker,
    TickerEvent,
    Trade,
    TradesEvent,
)
from perps_agent.venue.models import UtcDatetime

log = logging.getLogger(__name__)

FeedStatus = Literal["subscribed", "stale", "fresh", "resubscribing", "stopped"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: UtcDatetime
    instrument_id: int


class StreamRecord(_Record):
    type: Literal["stream"] = "stream"
    event: MarketEvent


class RestRecord(_Record):
    type: Literal["rest"] = "rest"
    book: OrderBook
    ticker: Ticker
    trades: tuple[Trade, ...]


class StatusRecord(_Record):
    type: Literal["status"] = "status"
    status: FeedStatus
    detail: str = ""


Record = Annotated[StreamRecord | RestRecord | StatusRecord, Field(discriminator="type")]
_RECORD: TypeAdapter[StreamRecord | RestRecord | StatusRecord] = TypeAdapter(Record)


def recording_path(directory: Path, symbol: str, at: datetime) -> Path:
    return directory / at.date().isoformat() / f"{symbol}.jsonl"


def read_recording(path: Path) -> Iterator[StreamRecord | RestRecord | StatusRecord]:
    """Replay a recording in file order. A torn final line (crash mid-write) is skipped."""
    with path.open() as handle:
        lines = handle.readlines()
    for number, line in enumerate(lines, start=1):
        try:
            yield _RECORD.validate_json(line)
        except ValidationError:
            if number == len(lines) and not line.endswith("\n"):
                log.warning("skipped torn last line", extra={"path": str(path)})
                return
            raise


class Recorder:
    def __init__(self, directory: Path, symbols: Mapping[int, str]) -> None:
        self._directory = directory
        self._symbols = dict(symbols)
        self._files: dict[int, tuple[Path, TextIO]] = {}
        # Last written book/ticker content per (instrument, kind), to skip repeats.
        self._last: dict[tuple[int, str], Any] = {}
        self.failed = False

    def stream(self, event: MarketEvent, instrument_id: int) -> None:
        key = _content_key(event)
        if key is not None:
            slot = (instrument_id, event.kind.value)
            if self._last.get(slot) == key:
                return
            self._last[slot] = key
        self._write(StreamRecord(at=event.received_at, instrument_id=instrument_id, event=event))

    def rest(
        self,
        instrument_id: int,
        *,
        book: OrderBook,
        ticker: Ticker,
        trades: tuple[Trade, ...],
        at: datetime,
    ) -> None:
        self._write(
            RestRecord(at=at, instrument_id=instrument_id, book=book, ticker=ticker, trades=trades)
        )

    def status(
        self, instrument_id: int, status: FeedStatus, at: datetime, detail: str = ""
    ) -> None:
        self._write(StatusRecord(at=at, instrument_id=instrument_id, status=status, detail=detail))
        if status in ("stale", "subscribed"):
            # Write the next book and ticker even if unchanged, so a replay sees
            # fresh state right after the gap.
            for slot in [s for s in self._last if s[0] == instrument_id]:
                del self._last[slot]

    def close(self) -> None:
        for _, handle in self._files.values():
            handle.close()
        self._files.clear()

    def _write(self, record: StreamRecord | RestRecord | StatusRecord) -> None:
        if self.failed:
            return
        try:
            handle = self._handle(record.instrument_id, record.at)
            handle.write(record.model_dump_json() + "\n")
        except OSError as error:
            self.failed = True
            log.error("recorder disabled: write failed", extra={"error": str(error)})
            self.close()

    def _handle(self, instrument_id: int, at: datetime) -> TextIO:
        path = recording_path(self._directory, self._symbols[instrument_id], at)
        current = self._files.get(instrument_id)
        if current is not None and current[0] == path:
            return current[1]
        if current is not None:
            current[1].close()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", buffering=1)
        self._files[instrument_id] = (path, handle)
        return handle


def _content_key(event: MarketEvent) -> Any:
    """What makes a book or ticker frame different from the last; None for other events."""
    match event:
        case BookEvent():
            return (event.book.bids, event.book.asks)
        case TickerEvent():
            return event.ticker.model_dump(exclude={"timestamp"})
        case BboEvent() | TradesEvent():
            return None


__all__ = [
    "FeedStatus",
    "Record",
    "Recorder",
    "RestRecord",
    "StatusRecord",
    "StreamRecord",
    "read_recording",
    "recording_path",
]
