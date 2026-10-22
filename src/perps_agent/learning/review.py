"""The daily review's tools (overview §3.13, ADR-0010): ``perps-agent review <tool> '<json>'``.

After the session has ended and its results are written, the agent reviews
its decisions against what happened and updates its lessons notebook. These
tools work on the local results only (no engine, no market): the day's
journal, the earlier sessions' journals and the notebook.

* ``decisions``: the day's decisions with their outcomes, for review.
* ``review``: record which scenario played out, which assumptions held and
  whether the thesis held, as a journal ``review`` entry.
* ``lessons``, ``add_lesson``, ``add_evidence``, ``retire_lesson``: the
  notebook. Evidence must name a decision in the local journals.
* ``record``: results by read and calibration over recent sessions.

Each answer is ``{"ok": true, "result": ...}`` or ``{"ok": false, "error": ...}``.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from perps_agent.calendar import NyseCalendar
from perps_agent.config.models import AppConfig
from perps_agent.journal import (
    Action,
    AssumptionCheck,
    DecisionTrail,
    Journal,
    JournalError,
    JsonlJournalSink,
    journal_path,
    load_journal,
    load_journals,
)
from perps_agent.journal.entries import Key, Text
from perps_agent.learning.lessons import LessonError, LessonStatus, open_lessons
from perps_agent.learning.record import Record, build_record
from perps_agent.strategy.history import prior_sessions

Json = dict[str, Any]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DayArgs(_Args):
    # The session to review; omitted: the newest local session.
    date: dt.date | None = None


class ReviewArgs(DayArgs):
    decision_id: str
    # The scenario that played out; null when none of them did.
    realized_scenario: Key | None
    assumptions: tuple[AssumptionCheck, ...] = ()
    thesis_held: bool | None = None
    notes: str | None = None


class LessonsArgs(_Args):
    status: LessonStatus | None = None


class EvidenceItem(_Args):
    decision_id: str
    supports: bool
    note: str | None = None


class AddLessonArgs(DayArgs):
    statement: Text
    setup: Key | None = None
    regime: Key | None = None
    symbol: str | None = None
    evidence: tuple[EvidenceItem, ...] = Field(default=(), max_length=20)


class AddEvidenceArgs(EvidenceItem):
    lesson_id: str


class RetireArgs(_Args):
    lesson_id: str
    reason: Text


class NoArgs(_Args):
    pass


@dataclass(frozen=True, slots=True)
class ReviewTool:
    name: str
    description: str
    schema: type[_Args]
    run: Callable[["ReviewDesk", Any], Json | list[Json]]


class ReviewDesk:
    """The review tools over one results directory."""

    def __init__(self, config: AppConfig, exchange: NyseCalendar, *, today: date) -> None:
        self._config = config
        self._exchange = exchange
        self._today = today
        self._results = Path(config.agent.results_dir)

    def call(self, tool: str, args: Json) -> Json:
        spec = TOOLS.get(tool)
        if tool == "tools":
            return {"ok": True, "result": describe_tools()}
        if spec is None:
            return {"ok": False, "error": f"unknown tool {tool}; 'tools' lists them"}
        try:
            parsed = spec.schema.model_validate(args)
            return {"ok": True, "result": spec.run(self, parsed)}
        except ValidationError as error:
            return {"ok": False, "error": f"invalid arguments: {error}"}
        except (JournalError, LessonError, ValueError) as error:
            return {"ok": False, "error": str(error)}

    # --- Tools ---

    def decisions(self, args: DayArgs) -> Json:
        day = self._day(args.date)
        journal = Journal.from_entries(load_journal(journal_path(self._results, day)))
        trails = [t for t in journal.decisions if t.decision.action is not Action.NOTE]
        return {
            "date": day.isoformat(),
            "unreviewed": len(journal.unreviewed()),
            "decisions": [self._decision_view(t) for t in trails],
        }

    def review(self, args: ReviewArgs) -> Json:
        day = self._day(args.date)
        path = journal_path(self._results, day)
        journal = Journal.from_entries(load_journal(path))
        if journal.decision(args.decision_id) is None:
            raise ValueError(f"decision {args.decision_id} is not in the {day} journal")
        sink = JsonlJournalSink(path)
        try:
            journal.attach(sink)
            entry = journal.review(
                args.decision_id,
                realized_scenario=args.realized_scenario,
                assumptions=args.assumptions,
                thesis_held=args.thesis_held,
                notes=args.notes,
            )
        finally:
            sink.close()
        return {"decision_id": entry.decision_id, "unreviewed": len(journal.unreviewed())}

    def lessons(self, args: LessonsArgs) -> list[Json]:
        book = open_lessons(self._results, self._config.learning)
        statuses = [args.status] if args.status else list(LessonStatus)
        return [lsn.view(book.rules) for s in statuses for lsn in book.by_status(s)]

    def add_lesson(self, args: AddLessonArgs) -> Json:
        known = self._known_decisions()
        for item in args.evidence:
            self._check_decision(item.decision_id, known)
        book = open_lessons(self._results, self._config.learning, writable=True)
        lesson = book.add(
            args.statement,
            day=self._day(args.date),
            setup=args.setup,
            regime=args.regime,
            symbol=args.symbol,
        )
        for item in args.evidence:
            book.add_evidence(
                lesson.lesson_id, item.decision_id, supports=item.supports, note=item.note
            )
        return lesson.view(book.rules)

    def add_evidence(self, args: AddEvidenceArgs) -> Json:
        self._check_decision(args.decision_id, self._known_decisions())
        book = open_lessons(self._results, self._config.learning, writable=True)
        if book.lesson(args.lesson_id) is None:
            raise LessonError(f"unknown lesson {args.lesson_id}")
        lesson = book.add_evidence(
            args.lesson_id, args.decision_id, supports=args.supports, note=args.note
        )
        return lesson.view(book.rules)

    def retire_lesson(self, args: RetireArgs) -> Json:
        book = open_lessons(self._results, self._config.learning, writable=True)
        if book.lesson(args.lesson_id) is None:
            raise LessonError(f"unknown lesson {args.lesson_id}")
        return book.retire(args.lesson_id, args.reason).view(book.rules)

    def record(self, _: NoArgs) -> Json:
        return record_view(self.recent_record())

    # --- Helpers ---

    def recent_record(self) -> Record:
        sessions = self._config.learning.record_sessions
        days = prior_sessions(self._exchange, self._today, sessions - 1)
        if journal_path(self._results, self._today).exists():
            days = [*days, self._today]
        return build_record(self._results, days)

    def _day(self, day: date | None) -> date:
        if day is not None:
            if not journal_path(self._results, day).exists():
                raise ValueError(f"no journal for {day}")
            return day
        days = sorted(p.parent.name for p in self._results.glob("*/journal.jsonl"))
        if not days:
            raise ValueError("no session journal in the results directory")
        return date.fromisoformat(days[-1])

    def _known_decisions(self) -> frozenset[str]:
        return frozenset(
            t.decision_id for t in Journal.from_entries(load_journals(self._results)).decisions
        )

    @staticmethod
    def _check_decision(decision_id: str, known: frozenset[str]) -> None:
        if decision_id not in known:
            raise ValueError(f"decision {decision_id} is not in the local journals")

    def _decision_view(self, trail: DecisionTrail) -> Json:
        d = trail.decision
        zone = self._config.session.zone
        view: Json = {
            "decision_id": d.decision_id,
            "at": d.at.astimezone(zone).strftime("%H:%M:%S"),
            "action": d.action,
            "symbol": d.symbol,
            "setup": d.setup,
            "regime": d.regime,
            "trigger": d.trigger.detail,
            "thesis": d.thesis,
            "reviewed": trail.review is not None,
        }
        if d.intent is not None:
            view["side"] = d.intent.direction
            view["confidence"] = str(d.intent.confidence)
        if d.scenarios:
            view["scenarios"] = [
                {
                    "name": s.name,
                    "probability": str(s.probability),
                    "expected_r": None if s.expected_r is None else str(s.expected_r),
                    "assumptions": [a.key for a in s.assumptions],
                }
                for s in d.scenarios
            ]
            view["chosen_scenario"] = d.chosen_scenario
        if d.action.opens:
            view["filled"] = bool(trail.outcomes) or bool(trail.orders)
        if trail.verdict is not None and not trail.verdict.approved:
            view["rejected"] = "; ".join(r.detail for r in trail.verdict.reasons)
        if trail.outcomes:
            view["outcomes"] = [
                {
                    "net_r": None if o.r_multiple is None else f"{o.r_multiple:.2f}",
                    "net_pnl": f"{o.net_pnl:.2f}",
                    "exit_reason": o.exit_reason,
                    "minutes": round(o.holding_seconds / 60),
                    "entry": str(o.average_entry),
                    "exit": str(o.average_exit),
                }
                for o in trail.outcomes
            ]
        return view


def record_view(record: Record) -> Json:
    return {
        "sessions": len(record.sessions),
        "reviewed": record.reviewed,
        "unreviewed": record.unreviewed,
        "by_read": [
            {
                "setup": s.setup,
                "trades": s.trades,
                "sessions": len(s.sessions),
                "net_r": f"{s.net_r:+.2f}",
                "average_r": f"{s.average_r:+.2f}",
                "win_rate": f"{s.wins / s.trades:.0%}",
            }
            for s in record.reads
        ],
        "calibration": [
            {
                "chosen_probability": b.label,
                "decisions": b.decisions,
                "stated": f"{b.stated:.0%}",
                "played_out": f"{b.realized:.0%}",
            }
            for b in record.calibration
        ],
    }


TOOLS: dict[str, ReviewTool] = {
    t.name: t
    for t in (
        ReviewTool(
            "decisions",
            "The session's decisions with their outcomes (default: the newest session).",
            DayArgs,
            ReviewDesk.decisions,
        ),
        ReviewTool(
            "review",
            "Record which scenario played out, which assumptions held, and whether the thesis "
            "held, for one decision.",
            ReviewArgs,
            ReviewDesk.review,
        ),
        ReviewTool(
            "lessons",
            "The lessons notebook, optionally one status.",
            LessonsArgs,
            ReviewDesk.lessons,
        ),
        ReviewTool(
            "add_lesson",
            "Add a candidate lesson, optionally with its first evidence.",
            AddLessonArgs,
            ReviewDesk.add_lesson,
        ),
        ReviewTool(
            "add_evidence",
            "File a reviewed decision as evidence for (supports) or against a lesson.",
            AddEvidenceArgs,
            ReviewDesk.add_evidence,
        ),
        ReviewTool(
            "retire_lesson",
            "Retire a lesson, with the reason.",
            RetireArgs,
            ReviewDesk.retire_lesson,
        ),
        ReviewTool(
            "record",
            "Results by read and calibration over recent sessions.",
            NoArgs,
            ReviewDesk.record,
        ),
    )
}


def describe_tools() -> list[Json]:
    return [
        {"name": t.name, "description": t.description, "input": t.schema.model_json_schema()}
        for t in TOOLS.values()
    ]
