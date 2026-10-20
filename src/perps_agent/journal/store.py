"""Append-only JSONL storage for the journal: ``results/<YYYY-MM-DD>/journal.jsonl``.

One entry per line, flushed as it is written, Decimals as strings. Known secret
values are replaced before a line is written (overview §7), so a thesis that
quotes one by mistake never reaches the results branch.
"""

import logging
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from perps_agent.journal.entries import JournalEntry
from perps_agent.log import REDACTED

log = logging.getLogger(__name__)

JOURNAL_FILE = "journal.jsonl"

_ADAPTER: TypeAdapter[JournalEntry] = TypeAdapter(JournalEntry)


def journal_path(results_dir: Path, day: date) -> Path:
    return results_dir / day.isoformat() / JOURNAL_FILE


class JsonlJournalSink:
    """Pass as ``Journal(sink=...)``: appends and flushes each entry as it is recorded."""

    def __init__(self, path: Path, *, secrets: Iterable[str] = ()) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Longest first so a secret containing another secret is fully replaced.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._file = path.open("a", encoding="utf-8")

    def __call__(self, entry: JournalEntry) -> None:
        line = _ADAPTER.dump_json(entry).decode()
        for secret in self._secrets:
            line = line.replace(secret, REDACTED)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def load_journal(path: Path) -> list[JournalEntry]:
    """Read one session's journal. A torn final line (crash mid-write) is skipped."""
    with path.open(encoding="utf-8") as handle:
        lines = handle.readlines()
    entries = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entries.append(_ADAPTER.validate_json(line))
        except ValidationError:
            if number == len(lines) and not line.endswith("\n"):
                log.warning("skipped torn last line", extra={"path": str(path)})
                break
            raise
    return entries


def load_journals(results_dir: Path) -> list[JournalEntry]:
    """Every session's journal under ``results_dir``, in date order."""
    entries: list[JournalEntry] = []
    for path in sorted(results_dir.glob(f"*/{JOURNAL_FILE}")):
        entries.extend(load_journal(path))
    return entries
