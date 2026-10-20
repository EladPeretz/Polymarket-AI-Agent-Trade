"""Append-only JSONL storage for ledger events, one event per line.

Decimals are written as strings, so a reloaded ledger is exact.
"""

from pathlib import Path

from pydantic import TypeAdapter

from perps_agent.ledger.events import LedgerEvent

_ADAPTER: TypeAdapter[LedgerEvent] = TypeAdapter(LedgerEvent)


class JsonlLedgerSink:
    """Pass as ``Ledger(sink=...)``: appends and flushes each event as it is recorded."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def __call__(self, event: LedgerEvent) -> None:
        self._file.write(_ADAPTER.dump_json(event).decode() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def load_events(path: Path) -> list[LedgerEvent]:
    with path.open(encoding="utf-8") as lines:
        return [_ADAPTER.validate_json(line) for line in lines if line.strip()]
