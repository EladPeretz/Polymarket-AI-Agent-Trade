import json
from datetime import timedelta
from decimal import Decimal
from itertools import count
from typing import Any

import pytest
from pydantic import ValidationError

from perps_agent.config.models import Mode
from perps_agent.journal import (
    Action,
    Assumption,
    AssumptionCheck,
    DecisionMade,
    Journal,
    JournalError,
    JsonlJournalSink,
    Scenario,
    TradeOutcome,
    Trigger,
    TriggerKind,
    journal_path,
    load_journal,
    load_journals,
    snapshot_ref,
)
from perps_agent.log import REDACTED
from perps_agent.risk import Direction, TradeIntent
from tests.unit.execution_factories import Desk
from tests.unit.risk_factories import NOW, intent, snapshot

D = Decimal
SYMBOL = "NVDA-USD"
DAY = NOW.date()

BREAKOUT = Scenario(
    name="breakout_holds",
    description="price holds above the opening range high and trends",
    probability=D("0.55"),
    expected_r=D("1.8"),
    assumptions=(
        Assumption(key="orb_volume_confirms", statement="breakout volume is above average"),
        Assumption(key="tech_bid", statement="semis stay bid with the index"),
    ),
)
FAILS = Scenario(
    name="false_breakout",
    description="price falls back into the range",
    probability=D("0.45"),
    expected_r=D(-1),
    assumptions=(Assumption(key="tech_bid", statement="semis stay bid with the index"),),
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> Any:
        self.now += timedelta(seconds=1)
        return self.now


def _journal(**kwargs: Any) -> Journal:
    ids = count(1)
    return Journal(clock=Clock(), new_id=lambda: f"{next(ids):032x}", **kwargs)


def _enter(journal: Journal, trade: TradeIntent | None = None, **fields: Any) -> DecisionMade:
    values: dict[str, Any] = {
        "trigger": Trigger(kind=TriggerKind.PLAYBOOK, detail="orb fired", playbook="orb@1"),
        "action": Action.ENTER,
        "symbol": SYMBOL,
        "snapshot": snapshot_ref(snapshot(), {"range_15m_bp": D(42)}),
        "regime": "trend",
        "setup": "orb",
        "thesis": "opening range breakout on volume",
        "scenarios": (BREAKOUT, FAILS),
        "chosen_scenario": "breakout_holds",
        "expected_r": D("0.54"),
        "intent": trade or intent(entry=None, stop="199"),
    }
    values.update(fields)
    return journal.decide(**values)


def _stand_aside(journal: Journal, **fields: Any) -> DecisionMade:
    values: dict[str, Any] = {
        "trigger": Trigger(kind=TriggerKind.CHECK_IN, detail="15-minute check-in"),
        "action": Action.STAND_ASIDE,
        "thesis": "no setup; chop around VWAP",
    }
    values.update(fields)
    return journal.decide(**values)


# --- A decision from intent to outcome ---


def test_a_trade_is_journaled_from_decision_to_outcome():
    desk = Desk()
    journal = _journal()
    journal.start_session(DAY, Mode.PAPER, [SYMBOL])
    desk.market(0)
    decision = _enter(journal)
    verdict = desk.evaluate(decision.intent)
    journal.record_verdict(decision.decision_id, verdict)
    state = desk.run(desk.execution.enter(verdict))
    link = journal.link_order(decision.decision_id, state.request.client_order_id, desk.ledger)
    desk.drain()
    desk.market(300)
    assert journal.record_outcomes(desk.ledger) == []  # still open

    desk.run(desk.execution.close_position(SYMBOL, "target reached early"))
    desk.drain()
    desk.market(600, mark="200.5", bid="200.48", ask="200.52")
    [outcome] = journal.record_outcomes(desk.ledger)
    assert journal.record_outcomes(desk.ledger) == []  # once per position

    position = desk.ledger.positions[0]
    assert link.position_id == position.position_id
    assert outcome.decision_id == decision.decision_id
    assert outcome.direction is Direction.LONG and outcome.playbook == "orb@1"
    assert outcome.net_pnl == position.net_pnl
    assert outcome.approved_risk == D("3.5")
    assert outcome.r_multiple == position.net_pnl / D("3.5")
    assert outcome.average_entry == position.average_entry
    assert outcome.average_exit == D("200.48")  # the IOC filled at the bid
    assert outcome.exit_reason == "target reached early"
    assert outcome.holding_seconds == pytest.approx(0.35, abs=0.1)

    trail = journal.decision(decision.decision_id)
    assert trail is not None
    assert trail.verdict == verdict
    assert trail.position_ids == (position.position_id,)
    assert trail.outcomes == [outcome]
    assert journal.unreviewed() == (trail,)

    journal.review(
        decision.decision_id,
        realized_scenario="false_breakout",
        assumptions=(
            AssumptionCheck(key="orb_volume_confirms", held=False),
            AssumptionCheck(key="tech_bid", held=True),
        ),
        thesis_held=False,
    )
    assert journal.unreviewed() == ()
    assert outcome.sequence == 5
    assert [e.sequence for e in journal.entries] == list(range(1, 7))


def test_a_refused_action_is_recorded():
    journal = _journal()
    decision = _enter(journal)
    journal.record_failure(decision.decision_id, "NVDA-USD is being closed")
    trail = journal.decision(decision.decision_id)
    assert trail is not None and trail.failures[0].error == "NVDA-USD is being closed"


def test_notes_are_not_waiting_for_review():
    journal = _journal()
    _stand_aside(journal, action=Action.NOTE, thesis="breadth is weak this morning")
    assert journal.unreviewed() == ()


# --- What a decision must carry ---


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"scenarios": ()}, "needs the scenarios"),
        ({"chosen_scenario": None}, "needs its chosen scenario"),
        ({"chosen_scenario": "gap_fill"}, "not among the scenarios"),
        ({"scenarios": (BREAKOUT, BREAKOUT)}, "unique"),
        (
            {"scenarios": (BREAKOUT, FAILS.model_copy(update={"probability": D("0.5")}))},
            "more than 1",
        ),
        ({"symbol": "TSLA-USD"}, "does not match the intent"),
    ],
)
def test_an_entry_decision_is_validated(fields, error):
    with pytest.raises(ValidationError, match=error):
        _enter(_journal(), **fields)


def test_an_entry_needs_its_intent_and_a_pass_has_none():
    with pytest.raises(ValidationError, match="needs its trade intent"):
        _enter(_journal(), intent=None)
    with pytest.raises(ValidationError, match="does not carry a trade intent"):
        _stand_aside(_journal(), intent=intent())


def test_assumption_keys_are_stable_identifiers():
    with pytest.raises(ValidationError):
        Assumption(key="Volume Confirms!", statement="x")


def test_decision_assumptions_are_deduplicated_across_scenarios():
    decision = _enter(_journal())
    assert [a.key for a in decision.assumptions] == ["orb_volume_confirms", "tech_bid"]


# --- What the journal refuses ---


def test_entries_must_refer_to_a_known_decision():
    with pytest.raises(JournalError, match="unknown decision"):
        _journal().record_failure("f" * 32, "boom")


def test_a_verdict_must_be_for_the_decisions_intent():
    desk = Desk()
    desk.market(0)
    journal = _journal()
    decision = _enter(journal)
    other = desk.evaluate(intent(entry=None, stop="198"))
    with pytest.raises(JournalError, match="different intent"):
        journal.record_verdict(decision.decision_id, other)


def test_an_order_is_linked_once_and_must_be_in_the_ledger():
    desk = Desk()
    desk.market(0)
    journal = _journal()
    decision = _enter(journal)
    with pytest.raises(JournalError, match="not in the ledger"):
        journal.link_order(decision.decision_id, "0" * 32, desk.ledger)
    state = desk.run(desk.execution.enter(desk.evaluate(decision.intent)))
    journal.link_order(decision.decision_id, state.request.client_order_id, desk.ledger)
    with pytest.raises(JournalError, match="linked twice"):
        journal.link_order(decision.decision_id, state.request.client_order_id, desk.ledger)


def test_a_review_can_only_judge_what_the_decision_weighed():
    journal = _journal()
    decision = _enter(journal)
    with pytest.raises(JournalError, match="was not weighed"):
        journal.review(decision.decision_id, realized_scenario="gap_fill")
    with pytest.raises(JournalError, match="vwap_support are not in decision"):
        journal.review(
            decision.decision_id,
            realized_scenario=None,
            assumptions=(AssumptionCheck(key="vwap_support", held=True),),
        )
    with pytest.raises(ValidationError, match="checked once"):
        journal.review(
            decision.decision_id,
            realized_scenario=None,
            assumptions=(
                AssumptionCheck(key="tech_bid", held=True),
                AssumptionCheck(key="tech_bid", held=False),
            ),
        )
    assert journal.decision(decision.decision_id).reviews == []


# --- Evidence for the learning loop ---


def _reviewed(journal: Journal, held: bool, **fields: Any) -> None:
    decision = _enter(journal, **fields)
    journal.review(
        decision.decision_id,
        realized_scenario="breakout_holds" if held else "false_breakout",
        assumptions=(AssumptionCheck(key="orb_volume_confirms", held=held),),
    )


def test_assumption_checks_count_the_latest_review_of_each_decision():
    journal = _journal()
    for held in (True, False, False):
        _reviewed(journal, held)
    first = journal.decisions[0].decision_id
    # The weekly review corrects the first one.
    journal.review(
        first,
        realized_scenario="false_breakout",
        assumptions=(AssumptionCheck(key="orb_volume_confirms", held=False),),
    )
    checks = journal.assumption_checks("orb_volume_confirms")
    assert [c.check.held for c in checks] == [False, False, False]
    assert checks[-1].decision.decision_id == first  # ordered by review time
    assert len(journal.assumption_checks("orb_volume_confirms", last=2)) == 2
    assert journal.assumption_checks("tech_bid") == ()


def test_assumption_checks_can_be_narrowed_to_a_setup_or_regime():
    journal = _journal()
    _reviewed(journal, False)
    _reviewed(journal, True, regime="compressed")
    _reviewed(journal, True, setup="vwap_reversion")
    assert len(journal.assumption_checks("orb_volume_confirms", setup="orb")) == 2
    assert len(journal.assumption_checks("orb_volume_confirms", regime="trend")) == 2
    held = journal.assumption_checks("orb_volume_confirms", setup="orb", regime="trend")
    assert [c.check.held for c in held] == [False]


# --- Storage ---


def test_the_jsonl_journal_rebuilds_the_same_journal(tmp_path):
    path = journal_path(tmp_path, DAY)
    sink = JsonlJournalSink(path)
    journal = _journal(sink=sink)
    journal.start_session(DAY, Mode.PAPER, [SYMBOL])
    decision = _enter(journal)
    _stand_aside(journal)
    journal.review(decision.decision_id, realized_scenario="breakout_holds")
    sink.close()

    assert path == tmp_path / DAY.isoformat() / "journal.jsonl"
    entries = load_journal(path)
    assert entries == list(journal.entries)
    rebuilt = Journal.from_entries(entries)
    assert rebuilt.decision(decision.decision_id) == journal.decision(decision.decision_id)
    raw = json.loads(path.read_text().splitlines()[1])
    assert raw["type"] == "decision"
    assert raw["scenarios"][0]["probability"] == "0.55"  # Decimals stay exact


def test_journals_load_across_sessions_in_date_order(tmp_path):
    for offset in (1, 0):
        day = DAY + timedelta(days=offset)
        sink = JsonlJournalSink(journal_path(tmp_path, day))
        Journal(sink=sink).start_session(day, Mode.PAPER, [SYMBOL])
        sink.close()
    days = [e.day for e in load_journals(tmp_path)]
    assert days == [DAY, DAY + timedelta(days=1)]


def test_a_torn_last_line_is_skipped(tmp_path):
    path = tmp_path / "journal.jsonl"
    sink = JsonlJournalSink(path)
    journal = _journal(sink=sink)
    _stand_aside(journal)
    sink.close()
    with path.open("a") as handle:
        handle.write('{"type": "decision", "seq')
    assert len(load_journal(path)) == 1


def test_secrets_never_reach_the_journal(tmp_path):
    path = tmp_path / "journal.jsonl"
    sink = JsonlJournalSink(path, secrets=["s3cr3t-api-key"])
    journal = _journal(sink=sink)
    _stand_aside(journal, thesis="key s3cr3t-api-key pasted by mistake")
    sink.close()
    text = path.read_text()
    assert "s3cr3t-api-key" not in text
    assert REDACTED in text


def test_a_snapshot_digest_identifies_the_full_snapshot():
    ref = snapshot_ref(snapshot())
    assert ref == snapshot_ref(snapshot())
    assert ref.digest != snapshot_ref(snapshot(mark="201")).digest
    assert ref.mark_price == D(200) and ref.spread_bp == D(2) and not ref.stale


def test_outcomes_need_a_linked_position():
    journal = _journal()
    decision = _enter(journal)
    with pytest.raises(JournalError, match="not linked"):
        journal.record(
            TradeOutcome(
                at=NOW,
                decision_id=decision.decision_id,
                position_id="p1",
                symbol=SYMBOL,
                direction=Direction.LONG,
                level=None,
                playbook=None,
                opened_at=NOW,
                closed_at=NOW,
                max_quantity=D(1),
                average_entry=D(200),
                average_exit=D(201),
                realized_pnl=D(1),
                fees=D(0),
                funding=D(0),
                net_pnl=D(1),
                approved_risk=D(1),
                r_multiple=D(1),
                exit_reason=None,
            )
        )
