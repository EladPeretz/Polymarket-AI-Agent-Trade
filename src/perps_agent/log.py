"""Structured logging: one JSON object per line, UTC timestamps, secrets redacted."""

import json
import logging
import sys
from collections.abc import Iterable
from typing import Any

from perps_agent.clock import utc_now

REDACTED = "[REDACTED]"

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": utc_now().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class RedactingFormatter(logging.Formatter):
    """Wraps another formatter and replaces known secret values in its output."""

    def __init__(self, inner: logging.Formatter, secrets: Iterable[str]) -> None:
        super().__init__()
        self._inner = inner
        # Longest first so a secret containing another secret is fully replaced.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = self._inner.format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(level: str = "INFO", *, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(JsonFormatter(), secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
