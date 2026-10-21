"""Incidents (overview §3.10, §3.12): what went wrong in a session, and when.

An incident is appended to ``results/<date>/incidents.jsonl`` and shown at the
top of the daily report. Warnings and critical incidents also go to the
alert, which logs them (owner decision 2026-10-21: logs, incidents file and
report only in paper mode; a push channel is added before the live pilot).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from perps_agent.clock import utc_now
from perps_agent.venue.models import UtcDatetime

log = logging.getLogger(__name__)

INCIDENTS_FILE = "incidents.jsonl"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentKind(StrEnum):
    WINDOW_CLOSE = "window_close"
    CLOSE_FAILED = "close_failed"
    NOT_FLAT = "not_flat"
    KILL_SWITCH = "kill_switch"
    AGENT_UNRESPONSIVE = "agent_unresponsive"
    AGENT_RESUMED = "agent_resumed"
    STALE_DATA = "stale_data"
    DATA_RECOVERED = "data_recovered"
    EXECUTION = "execution"
    LEDGER_MISMATCH = "ledger_mismatch"
    ENGINE_ERROR = "engine_error"
    PUBLISH_FAILED = "publish_failed"


class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["incident"] = "incident"
    sequence: int = 0
    at: UtcDatetime
    kind: IncidentKind
    severity: Severity
    detail: str
    symbol: str | None = None


_ADAPTER: TypeAdapter[Incident] = TypeAdapter(Incident)


def log_alert(incident: Incident) -> None:
    """The paper-mode alert: a log line marked ``alert``."""
    level = logging.CRITICAL if incident.severity is Severity.CRITICAL else logging.WARNING
    log.log(
        level,
        incident.detail,
        extra={"alert": True, "incident": str(incident.kind), "symbol": incident.symbol},
    )


class IncidentLog:
    """The session's incidents. ``sink`` stores each one; ``alert`` hears warnings and worse."""

    def __init__(
        self,
        *,
        sink: Callable[[Incident], None] | None = None,
        alert: Callable[[Incident], None] = log_alert,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sink = sink
        self._alert = alert
        self._clock = clock
        self._incidents: list[Incident] = []

    def record(
        self,
        kind: IncidentKind,
        severity: Severity,
        detail: str,
        *,
        symbol: str | None = None,
        at: datetime | None = None,
    ) -> Incident:
        incident = Incident(
            sequence=len(self._incidents) + 1,
            at=at if at is not None else self._clock(),
            kind=kind,
            severity=severity,
            detail=detail,
            symbol=symbol,
        )
        self._incidents.append(incident)
        if self._sink is not None:
            self._sink(incident)
        if severity is not Severity.INFO:
            self._alert(incident)
        else:
            log.info(detail, extra={"incident": str(kind), "symbol": symbol})
        return incident

    @property
    def incidents(self) -> tuple[Incident, ...]:
        return tuple(self._incidents)


class JsonlIncidentSink:
    """Pass as ``IncidentLog(sink=...)``: appends and flushes each incident."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def __call__(self, incident: Incident) -> None:
        self._file.write(_ADAPTER.dump_json(incident).decode() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def append_incident(path: Path, incident: Incident) -> None:
    """Add one incident to a day's file outside a session (e.g. a failed publish)."""
    sink = JsonlIncidentSink(path)
    try:
        sink(incident)
    finally:
        sink.close()


def load_incidents(path: Path) -> list[Incident]:
    with path.open(encoding="utf-8") as lines:
        return [_ADAPTER.validate_json(line) for line in lines if line.strip()]
