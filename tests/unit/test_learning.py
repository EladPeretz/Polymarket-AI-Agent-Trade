from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from perps_agent.agent import build_briefing
from perps_agent.config.models import LearningConfig
from perps_agent.journal import JsonlJournalSink, journal_path
from perps_agent.learning import (
    LessonBook,
    LessonError,
    LessonStatus,
    ReviewDesk,
    build_record,
    lessons_path,
    open_lessons,
)
from tests.unit.agent_factories import CONFIG, PLAYBOOKS, AgentDesk, proposal
from tests.unit.strategy_factories import CALENDAR, WINDOW

DAY = date(2026, 10, 21)
NEXT_DAY = date(2026, 10, 22)
RULES = LearningConfig()


def _ids() -> Iterator[str]:
    return iter(f"{n:032x}" for n in range(1, 100))


def _session(results: Path, day: date = DAY, *, setup: str = "trapped_shorts_cover") -> str:
    """A journal with one closed discretionary trade; returns the entry's decision id."""
    desk = AgentDesk()
    desk.market(0)
    answer = desk.call("propose_trade", **proposal(setup=setup))
    desk.market(1000)
    desk.drain()
    desk.call("close_position", symbol="NVDA-USD", reason="done")
    desk.market(2000)
    desk.drain()
    desk.sync()
    desk.journal.record_outcomes(desk.execution.ledger)
    sink = JsonlJournalSink(journal_path(results, day))
    for entry in desk.journal.entries:
        sink(entry)
    sink.close()
    return str(answer["decision_id"])


def _desk(results: Path, today: date = DAY) -> ReviewDesk:
    config = CONFIG.model_copy(
        update={"agent": CONFIG.agent.model_copy(update={"results_dir": str(results)})}
    )
    return ReviewDesk(config, CALENDAR.exchange, today=today)


def _ok(answer: dict) -> object:
    assert answer["ok"], answer
    return answer["result"]


# --- Lessons notebook ---


def test_a_lesson_is_active_only_with_repeated_support():
    ids = _ids()
    book = LessonBook(RULES)
    lesson = book.add("shorts pressing a prior low get squeezed", day=DAY, setup="squeeze")
    assert lesson.status(RULES) is LessonStatus.CANDIDATE
    for _ in range(2):
        book.add_evidence(lesson.lesson_id, next(ids), supports=True)
    assert lesson.status(RULES) is LessonStatus.CANDIDATE
    book.add_evidence(lesson.lesson_id, next(ids), supports=True)
    assert lesson.status(RULES) is LessonStatus.ACTIVE
    # One against in four is within the allowed share; two in five is not.
    book.add_evidence(lesson.lesson_id, next(ids), supports=False)
    assert lesson.status(RULES) is LessonStatus.ACTIVE
    book.add_evidence(lesson.lesson_id, next(ids), supports=False)
    assert lesson.status(RULES) is LessonStatus.RETIRED


def test_early_contradiction_keeps_a_lesson_a_candidate():
    ids = _ids()
    book = LessonBook(RULES)
    lesson = book.add("gap fades fail on trend days", day=DAY)
    book.add_evidence(lesson.lesson_id, next(ids), supports=False)
    assert lesson.status(RULES) is LessonStatus.CANDIDATE
    assert book.by_status(LessonStatus.CANDIDATE) == [lesson]


def test_evidence_counts_once_and_retired_lessons_take_no_more():
    ids = _ids()
    decision = next(ids)
    book = LessonBook(RULES)
    lesson = book.add("x", day=DAY)
    book.add_evidence(lesson.lesson_id, decision, supports=True)
    with pytest.raises(LessonError, match="already evidence"):
        book.add_evidence(lesson.lesson_id, decision, supports=False)
    book.retire(lesson.lesson_id, "the market changed")
    assert lesson.status(RULES) is LessonStatus.RETIRED
    with pytest.raises(LessonError, match="retired"):
        book.add_evidence(lesson.lesson_id, next(ids), supports=True)
    with pytest.raises(LessonError, match="unknown lesson"):
        book.retire("L00000000", "?")


def test_the_notebook_is_an_append_only_file(tmp_path):
    ids = _ids()
    book = open_lessons(tmp_path, RULES, writable=True)
    lesson = book.add("BTC leads MSTR at the open", day=DAY, symbol="MSTR-USD")
    book.add_evidence(lesson.lesson_id, next(ids), supports=True, note="clean")
    assert len(lessons_path(tmp_path).read_text().splitlines()) == 2
    again = open_lessons(tmp_path, RULES).lesson(lesson.lesson_id)
    assert again is not None
    assert (again.supports, again.added.symbol) == (1, "MSTR-USD")


# --- Review tools ---


def test_review_lists_decisions_and_records_a_review(tmp_path):
    decision_id = _session(tmp_path)
    desk = _desk(tmp_path)
    listing = _ok(desk.call("decisions", {}))
    assert listing["date"] == DAY.isoformat()
    entry = next(d for d in listing["decisions"] if d["decision_id"] == decision_id)
    assert entry["setup"] == "trapped_shorts_cover"
    assert entry["chosen_scenario"] == "bounce"
    assert entry["outcomes"][0]["exit_reason"]
    assert not entry["reviewed"]

    result = _ok(
        desk.call(
            "review",
            {
                "decision_id": decision_id,
                "realized_scenario": "flush",
                "assumptions": [{"key": "vwap_holds", "held": False}],
                "thesis_held": False,
                "notes": "sellers kept pressing",
            },
        )
    )
    assert result["decision_id"] == decision_id
    reviewed = _ok(desk.call("decisions", {"date": DAY.isoformat()}))["decisions"]
    assert next(d for d in reviewed if d["decision_id"] == decision_id)["reviewed"]


def test_review_refuses_what_the_decision_did_not_weigh(tmp_path):
    decision_id = _session(tmp_path)
    desk = _desk(tmp_path)
    answer = desk.call("review", {"decision_id": decision_id, "realized_scenario": "melt_up"})
    assert not answer["ok"]
    assert "not weighed" in answer["error"]
    assert not desk.call("review", {"decision_id": "0" * 32, "realized_scenario": None})["ok"]
    assert not desk.call("nope", {})["ok"]
    assert {t["name"] for t in _ok(desk.call("tools", {}))} >= {"review", "add_lesson"}


def test_lessons_need_evidence_from_the_journals(tmp_path):
    decision_id = _session(tmp_path)
    desk = _desk(tmp_path)
    lesson = _ok(
        desk.call(
            "add_lesson",
            {
                "statement": "a broken low bought straight back squeezes shorts",
                "setup": "trapped_shorts_cover",
                "evidence": [{"decision_id": decision_id, "supports": False}],
            },
        )
    )
    assert (lesson["status"], lesson["contradicts"], lesson["since"]) == (
        "candidate",
        1,
        DAY.isoformat(),
    )
    unknown = desk.call(
        "add_evidence", {"lesson_id": lesson["id"], "decision_id": "f" * 32, "supports": True}
    )
    assert not unknown["ok"]
    assert "not in the local journals" in unknown["error"]
    twice = desk.call(
        "add_evidence", {"lesson_id": lesson["id"], "decision_id": decision_id, "supports": True}
    )
    assert not twice["ok"]
    listed = _ok(desk.call("lessons", {"status": "candidate"}))
    assert [entry["id"] for entry in listed] == [lesson["id"]]
    retired = _ok(desk.call("retire_lesson", {"lesson_id": lesson["id"], "reason": "no edge"}))
    assert retired["status"] == "retired"


# --- Record and briefing ---


def test_record_groups_trades_by_read_and_scores_calibration(tmp_path):
    decision_id = _session(tmp_path)
    _session(tmp_path, NEXT_DAY, setup="btc_led_breakout")
    _ok(
        _desk(tmp_path).call(
            "review",
            {"decision_id": decision_id, "realized_scenario": "bounce", "date": DAY.isoformat()},
        )
    )
    record = build_record(tmp_path, [DAY, NEXT_DAY, date(2026, 10, 20)])
    assert record.sessions == (DAY, NEXT_DAY)
    assert sorted(s.setup for s in record.reads) == ["btc_led_breakout", "trapped_shorts_cover"]
    assert all(s.trades == 1 for s in record.reads)
    assert (record.reviewed, record.unreviewed) == (1, 1)
    (bucket,) = record.calibration
    assert (bucket.label, bucket.decisions, bucket.stated, bucket.realized) == (
        "60% and over",
        1,
        Decimal("0.6"),
        Decimal(1),
    )


def test_briefing_shows_lessons_and_the_record(tmp_path):
    ids = _ids()
    decision_id = _session(tmp_path)
    book = open_lessons(tmp_path, RULES, writable=True)
    proven = book.add("dips into VWAP get bought on trend days", day=DAY, setup="vwap_dip")
    for _ in range(3):
        book.add_evidence(proven.lesson_id, next(ids), supports=True)
    book.add("oil fades the EIA spike", day=DAY, symbol="WTIOIL-USD")
    record = build_record(tmp_path, [DAY])
    text = build_briefing(
        CONFIG,
        day=NEXT_DAY,
        window=WINDOW,
        playbooks=PLAYBOOKS,
        screened=(),
        activity=(),
        lessons=book,
        record=record,
    )
    assert "## Lessons and your record" in text
    assert f"`{proven.lesson_id}` dips into VWAP get bought on trend days (setup vwap_dip" in text
    assert "Candidates (unproven" in text
    assert "oil fades the EIA spike (symbol WTIOIL-USD; 0 for, 0 against)" in text
    assert "| trapped_shorts_cover | 1 |" in text
    assert "1 entries in these sessions were never reviewed." in text
    assert decision_id not in text


def test_briefing_without_history_says_so():
    text = build_briefing(
        CONFIG, day=NEXT_DAY, window=WINDOW, playbooks=PLAYBOOKS, screened=(), activity=()
    )
    assert "No lessons yet." in text
    assert "No earlier session journals for a record yet." in text
