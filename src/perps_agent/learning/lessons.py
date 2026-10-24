"""The lessons notebook (overview §3.13, ADR-0010): what the agent has learned, with evidence.

A lesson is a short statement about how the market behaves (``"MSTR shorts
pressing a prior low get squeezed when BTC is bid"``), tagged by the read
(``setup`` key), regime and symbol it is about. It starts as a **candidate**,
a hypothesis. Each reviewed decision can be filed as evidence for or against
it, once. It becomes **active**, used in decisions, when at least
``learning.min_support`` decisions support it and at most
``learning.max_contradiction_share`` of its evidence contradicts it. More
contradiction than that **retires** it, as does the agent. The thresholds are
defined in ``main``; the agent cannot change them.

The notebook is an append-only event log, ``results/lessons/lessons.jsonl``,
so every change can be read back and reverted. Evidence must name a decision
in the local journals.
"""

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from perps_agent.clock import utc_now
from perps_agent.config.models import LearningConfig
from perps_agent.journal.entries import Key, Text
from perps_agent.venue.models import UtcDatetime

LESSONS_DIR = "lessons"
LESSONS_FILE = "lessons.jsonl"

LessonId = Annotated[str, Field(pattern=r"^L[0-9a-f]{8}$")]
DecisionId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class LessonError(ValueError):
    """A change the notebook refuses: unknown lesson or decision, duplicate evidence."""


class LessonStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    RETIRED = "retired"


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: UtcDatetime
    lesson_id: LessonId


class LessonAdded(_Event):
    type: Literal["added"] = "added"
    statement: Text
    setup: Key | None = None
    regime: Key | None = None
    symbol: str | None = None
    # The session the lesson was drawn from.
    day: date


class EvidenceAdded(_Event):
    type: Literal["evidence"] = "evidence"
    decision_id: DecisionId
    supports: bool
    note: str | None = None


class LessonRetired(_Event):
    type: Literal["retired"] = "retired"
    reason: Text


LessonEvent = Annotated[LessonAdded | EvidenceAdded | LessonRetired, Field(discriminator="type")]
_ADAPTER: TypeAdapter[LessonEvent] = TypeAdapter(LessonEvent)


@dataclass(slots=True)
class Lesson:
    added: LessonAdded
    evidence: list[EvidenceAdded] = field(default_factory=list)
    retired: LessonRetired | None = None

    @property
    def lesson_id(self) -> str:
        return self.added.lesson_id

    @property
    def supports(self) -> int:
        return sum(1 for e in self.evidence if e.supports)

    @property
    def contradicts(self) -> int:
        return sum(1 for e in self.evidence if not e.supports)

    @property
    def last_evidence_at(self) -> datetime:
        return self.evidence[-1].at if self.evidence else self.added.at

    def status(self, rules: LearningConfig) -> LessonStatus:
        if self.retired is not None:
            return LessonStatus.RETIRED
        total = len(self.evidence)
        if total and Decimal(self.contradicts) / total > rules.max_contradiction_share:
            # Too much against it to use; with enough evidence it is retired.
            if total >= rules.min_support:
                return LessonStatus.RETIRED
            return LessonStatus.CANDIDATE
        if self.supports >= rules.min_support:
            return LessonStatus.ACTIVE
        return LessonStatus.CANDIDATE

    def view(self, rules: LearningConfig) -> dict[str, object]:
        added = self.added
        view: dict[str, object] = {
            "id": self.lesson_id,
            "status": self.status(rules),
            "statement": added.statement,
            "supports": self.supports,
            "contradicts": self.contradicts,
            "since": added.day.isoformat(),
        }
        for name in ("setup", "regime", "symbol"):
            value = getattr(added, name)
            if value is not None:
                view[name] = value
        if self.retired is not None:
            view["retired_because"] = self.retired.reason
        return view


class LessonBook:
    """The notebook folded from its events. ``sink`` receives each new event."""

    def __init__(
        self,
        rules: LearningConfig,
        *,
        sink: Callable[[LessonEvent], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
        new_id: Callable[[], str] = lambda: "L" + uuid.uuid4().hex[:8],
    ) -> None:
        self.rules = rules
        self._sink = sink
        self._clock = clock
        self._new_id = new_id
        self._lessons: dict[str, Lesson] = {}

    def replay(self, events: Iterable[LessonEvent]) -> "LessonBook":
        """Apply saved events without passing them to the sink."""
        for event in events:
            self._apply(event)
        return self

    @property
    def lessons(self) -> tuple[Lesson, ...]:
        return tuple(self._lessons.values())

    def lesson(self, lesson_id: str) -> Lesson | None:
        return self._lessons.get(lesson_id)

    def by_status(self, status: LessonStatus) -> list[Lesson]:
        """Lessons with ``status``, the best supported (then most recent) first."""
        found = [lsn for lsn in self._lessons.values() if lsn.status(self.rules) is status]
        found.sort(key=lambda lsn: (lsn.supports - lsn.contradicts, lsn.last_evidence_at))
        return found[::-1]

    # --- Changes ---

    def add(
        self,
        statement: str,
        *,
        day: date,
        setup: str | None = None,
        regime: str | None = None,
        symbol: str | None = None,
    ) -> Lesson:
        event = LessonAdded(
            at=self._clock(),
            lesson_id=self._new_id(),
            statement=statement,
            setup=setup,
            regime=regime,
            symbol=symbol,
            day=day,
        )
        self._record(event)
        return self._lessons[event.lesson_id]

    def add_evidence(
        self, lesson_id: str, decision_id: str, *, supports: bool, note: str | None = None
    ) -> Lesson:
        event = EvidenceAdded(
            at=self._clock(),
            lesson_id=lesson_id,
            decision_id=decision_id,
            supports=supports,
            note=note,
        )
        self._record(event)
        return self._lessons[lesson_id]

    def retire(self, lesson_id: str, reason: str) -> Lesson:
        self._record(LessonRetired(at=self._clock(), lesson_id=lesson_id, reason=reason))
        return self._lessons[lesson_id]

    def _record(self, event: LessonEvent) -> None:
        self._apply(event)
        if self._sink is not None:
            self._sink(event)

    def _apply(self, event: LessonEvent) -> None:
        match event:
            case LessonAdded():
                if event.lesson_id in self._lessons:
                    raise LessonError(f"lesson {event.lesson_id} added twice")
                self._lessons[event.lesson_id] = Lesson(event)
            case EvidenceAdded():
                lesson = self._lesson(event.lesson_id)
                if lesson.retired is not None:
                    raise LessonError(f"lesson {event.lesson_id} is retired")
                if any(e.decision_id == event.decision_id for e in lesson.evidence):
                    raise LessonError(
                        f"decision {event.decision_id} is already evidence for {event.lesson_id}"
                    )
                lesson.evidence.append(event)
            case LessonRetired():
                lesson = self._lesson(event.lesson_id)
                if lesson.retired is not None:
                    raise LessonError(f"lesson {event.lesson_id} is already retired")
                lesson.retired = event

    def _lesson(self, lesson_id: str) -> Lesson:
        try:
            return self._lessons[lesson_id]
        except KeyError:
            raise LessonError(f"unknown lesson {lesson_id}") from None


def lessons_path(results_dir: Path) -> Path:
    return results_dir / LESSONS_DIR / LESSONS_FILE


class JsonlLessonSink:
    """Appends and flushes each notebook event."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def __call__(self, event: LessonEvent) -> None:
        with self.path.open("a", encoding="utf-8") as out:
            out.write(_ADAPTER.dump_json(event).decode() + "\n")


def load_lessons(path: Path) -> list[LessonEvent]:
    """The notebook's events; none if it does not exist. A torn final line is skipped."""
    if not path.exists():
        return []
    events = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            events.append(_ADAPTER.validate_json(line))
        except ValidationError:
            if number == len(lines):
                break
            raise
    return events


def open_lessons(results_dir: Path, rules: LearningConfig, *, writable: bool = False) -> LessonBook:
    path = lessons_path(results_dir)
    sink = JsonlLessonSink(path) if writable else None
    return LessonBook(rules, sink=sink).replay(load_lessons(path))
