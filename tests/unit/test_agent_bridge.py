import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from perps_agent.agent import ToolError
from perps_agent.journal import Action, DecisionMade, OrderLinked, RiskVerdictRecorded, TriggerKind
from perps_agent.risk import AutonomyLevel, Direction
from perps_agent.strategy import Trigger
from tests.unit.agent_factories import (
    CONFIG,
    SYMBOL,
    VWAP_TAG,
    AgentDesk,
    proposal,
    setup,
)
from tests.unit.risk_factories import NOW

D = Decimal


def _decisions(desk: AgentDesk) -> list[DecisionMade]:
    return [e for e in desk.journal.entries if isinstance(e, DecisionMade)]


def _enter(desk: AgentDesk, **overrides) -> dict:
    desk.market(0)
    result = desk.call("propose_trade", **proposal(**overrides))
    desk.market(300)
    return result


# --- Entries ---


def test_discretionary_proposal_is_l2_journaled_and_sent():
    desk = AgentDesk()
    result = _enter(desk)
    assert result["decision"] == "approved"
    assert result["level"] == "L2"
    assert result["order"]["status"]
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None and position.level is AutonomyLevel.L2
    kinds = [e.type for e in desk.journal.entries]
    assert kinds == ["decision", "risk_verdict", "order_linked"]
    decision = _decisions(desk)[0]
    assert decision.action is Action.ENTER
    assert decision.trigger.kind is TriggerKind.AGENT
    assert decision.chosen_scenario == "bounce"
    assert decision.setup == "discretionary"
    assert decision.snapshot is not None and "range_15m_bp" in decision.snapshot.features
    assert [a.key for a in decision.assumptions] == ["vwap_holds"]


def test_rejection_is_returned_with_reasons_and_journaled():
    desk = AgentDesk()
    desk.market(0)
    result = desk.call("propose_trade", **proposal(stop_price="199.9"))
    assert result["decision"] == "rejected"
    assert any(r.startswith("stop_distance") for r in result["reasons"])
    assert desk.ledger.open_position(SYMBOL) is None
    assert [e.type for e in desk.journal.entries] == ["decision", "risk_verdict"]


def _thin_book_for(desk: AgentDesk, calls: int) -> list[int]:
    """The view's first ``calls`` snapshots show a thin book; returns the call counter."""
    seen = [0]
    real = desk.view.snapshot

    def snapshot(symbol: str):
        seen[0] += 1
        snap = real(symbol)
        if seen[0] > calls:
            return snap
        thin = {
            side: (getattr(snap, side)[0].model_copy(update={"quantity": D(1)}),)
            for side in ("bids", "asks")
        }
        return snap.model_copy(update=thin)

    desk.view.snapshot = snapshot  # type: ignore[method-assign]
    return seen


FAST_RECHECK = CONFIG.model_copy(
    update={"agent": CONFIG.agent.model_copy(update={"market_recheck_interval_ms": 0})}
)


def test_a_thin_book_is_checked_again_before_rejecting():
    desk = AgentDesk(config=FAST_RECHECK)
    desk.market(0)
    _thin_book_for(desk, calls=2)  # the proposal's snapshot and the first recheck
    result = desk.call("propose_trade", **proposal())
    assert result["decision"] == "approved"
    assert result["market_rechecks"] == 2
    assert [e.type for e in desk.journal.entries] == ["decision", "risk_verdict", "order_linked"]


def test_a_book_that_stays_thin_is_rejected_after_the_rechecks():
    desk = AgentDesk(config=FAST_RECHECK)
    desk.market(0)
    _thin_book_for(desk, calls=100)
    result = desk.call("propose_trade", **proposal())
    assert result["decision"] == "rejected"
    assert result["market_rechecks"] == CONFIG.agent.market_recheck_attempts
    assert any(r.startswith("depth") for r in result["reasons"])


def test_hold_minutes_becomes_a_time_stop():
    desk = AgentDesk()
    _enter(desk, hold_minutes=30)
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None
    assert position.time_stop == NOW + timedelta(minutes=30)


def test_proposal_with_exit_plan_attaches_it():
    desk = AgentDesk()
    _enter(desk, exit_plan={"breakeven_after_r": "1"})
    plan = desk.plans.plan(SYMBOL)
    assert plan is not None and plan.breakeven_after_r == D(1)
    view = desk.call("get_portfolio")["positions"][0]
    assert view["exit_plan"] == "breakeven at 1R"


def test_invalid_arguments_are_explained():
    desk = AgentDesk()
    with pytest.raises(ToolError, match="limit_price"):
        desk.call("propose_trade", **proposal(order_type="limit"))
    with pytest.raises(ToolError, match="unknown tool"):
        desk.call("place_order")
    with pytest.raises(ToolError, match="not traded"):
        desk.call("get_snapshot", symbol="AAPL-USD")


# --- Playbooks and Gate 1 ---


def test_unscreened_playbook_cannot_trade_as_a_playbook():
    desk = AgentDesk()
    desk.view.live_setups = (setup(),)
    desk.market(0)
    with pytest.raises(ToolError, match="Gate 1"):
        desk.call("propose_trade", **proposal(source="playbook:vwap_reversion"))
    assert desk.journal.entries == ()


def test_playbook_needs_a_live_setup():
    desk = AgentDesk(screened=(VWAP_TAG,))
    desk.market(0)
    with pytest.raises(ToolError, match="no live long setup"):
        desk.call("propose_trade", **proposal(source="playbook:vwap_reversion"))


def test_screened_setup_as_is_is_l0_and_changed_is_l1():
    desk = AgentDesk(screened=(VWAP_TAG,))
    desk.view.live_setups = (setup(),)
    desk.market(0)
    as_is = proposal(
        source="playbook:vwap_reversion",
        order_type="limit",
        limit_price="200",
        stop_price="199",
        target_price="203",
    )
    result = desk.call("propose_trade", **as_is)
    assert result["level"] == "L0" and result["playbook"] == VWAP_TAG
    desk.run(desk.execution.close_position(SYMBOL, "test"))

    desk2 = AgentDesk(screened=(VWAP_TAG,))
    desk2.view.live_setups = (setup(),)
    desk2.market(0)
    changed = dict(as_is, stop_price="198.5")
    assert desk2.call("propose_trade", **changed)["level"] == "L1"


def test_size_above_the_setup_cap_is_refused():
    desk = AgentDesk(screened=(VWAP_TAG,))
    desk.view.live_setups = (setup(max_size=D("0.5")),)
    with pytest.raises(ToolError, match="cap"):
        desk.call("propose_trade", **proposal(source="playbook:vwap_reversion", size="0.8"))


def test_unscreened_setups_are_shown_labelled_or_hidden():
    desk = AgentDesk()
    desk.view.live_setups = (setup(),)
    shown = desk.call("get_snapshot", symbol=SYMBOL)["setups"]
    assert shown[0]["screened"] is False and shown[0]["playbook"] == VWAP_TAG

    hidden_config = CONFIG.model_copy(
        update={"agent": CONFIG.agent.model_copy(update={"unscreened_playbook_signals": False})}
    )
    desk = AgentDesk(config=hidden_config)
    desk.view.live_setups = (setup(),)
    assert desk.call("get_snapshot", symbol=SYMBOL)["setups"] == []
    trigger = Trigger(
        TriggerKind.PLAYBOOK, NOW, "playbook:vwap_reversion:long", "x", SYMBOL, setup()
    )
    desk.bridge.push([trigger])
    assert desk.call("next_event", timeout_seconds=0)["events"] == []


def test_list_playbooks_shows_gate_status_and_bounds():
    desk = AgentDesk(screened=(VWAP_TAG,))
    playbooks = {p["id"]: p for p in desk.call("list_playbooks")["playbooks"]}
    assert len(playbooks) == 6
    assert playbooks["vwap_reversion"]["screened"] is True
    assert playbooks["momentum_scalp"]["screened"] is False
    bounds = playbooks["vwap_reversion"]["parameters"]["min_target_r"]
    assert (bounds["min"], bounds["max"]) == ("2.1", "5")


# --- Events ---


def test_next_event_returns_queued_events_with_symbols_and_session():
    desk = AgentDesk()
    desk.market(0)
    trigger = Trigger(TriggerKind.LARGE_MOVE, NOW, "large_move:up", "+30 bp in 60s", SYMBOL)
    desk.bridge.push([trigger])
    answer = desk.call("next_event", timeout_seconds=0)
    assert [e["key"] for e in answer["events"]] == ["large_move:up"]
    assert answer["events"][0]["time"] == "10:00:00"
    assert answer["symbols"][0]["symbol"] == SYMBOL
    assert answer["symbols"][0]["features"]["volatility"]["range_15m_bp"] is not None
    assert answer["session"]["entries_allowed"] is True
    assert answer["session"]["minutes_to_entry_cutoff"] == 135
    assert desk.call("next_event", timeout_seconds=0)["events"] == []


def test_next_event_batches_and_reports_pending():
    config = CONFIG.model_copy(
        update={"agent": CONFIG.agent.model_copy(update={"max_events_per_call": 2})}
    )
    desk = AgentDesk(config=config)
    for n in range(3):
        desk.bridge.notify(TriggerKind.CHECK_IN, "check_in", f"check-in {n}")
    answer = desk.call("next_event", timeout_seconds=0)
    assert len(answer["events"]) == 2 and answer["pending"] == 1


def test_next_event_wakes_when_an_event_arrives():
    desk = AgentDesk()

    async def scenario() -> dict:
        waiting = asyncio.create_task(desk.bridge.call("next_event", {"timeout_seconds": 30}))
        await asyncio.sleep(0.01)
        desk.bridge.notify(TriggerKind.CHECK_IN, "check_in", "scheduled check-in")
        return await asyncio.wait_for(waiting, 2)

    answer = desk.run(scenario())
    assert answer["events"][0]["kind"] == "check_in"


def test_ending_the_session_releases_a_waiting_next_event():
    desk = AgentDesk()

    async def scenario() -> tuple[dict, dict]:
        waiting = asyncio.create_task(desk.bridge.call("next_event", {"timeout_seconds": 30}))
        await asyncio.sleep(0.01)
        desk.bridge.end_session()
        first = await asyncio.wait_for(waiting, 2)
        # Later calls no longer wait for their timeout either.
        later = await asyncio.wait_for(desk.bridge.call("next_event", {"timeout_seconds": 30}), 2)
        return first, later

    first, later = desk.run(scenario())
    assert [e["key"] for e in first["events"]] == ["session:ended"]
    assert later["events"] == []


def test_decision_answering_an_event_is_filed_against_it():
    desk = AgentDesk()
    desk.market(0)
    trigger = Trigger(TriggerKind.LARGE_MOVE, NOW, "large_move:up", "+30 bp in 60s", SYMBOL)
    desk.bridge.push([trigger])
    event_id = desk.call("next_event", timeout_seconds=0)["events"][0]["id"]
    desk.call("propose_trade", **proposal(event_id=event_id))
    decision = _decisions(desk)[0]
    assert decision.trigger.kind is TriggerKind.LARGE_MOVE
    assert decision.trigger.detail == "+30 bp in 60s"
    with pytest.raises(ToolError, match="unknown event"):
        desk.call("journal", note="x", event_id="e999")


def test_sync_reports_fills_and_closes():
    desk = AgentDesk()
    _enter(desk)
    desk.sync()
    keys = [e["key"] for e in desk.call("next_event", timeout_seconds=0)["events"]]
    assert keys == ["order:entry_filled"]
    desk.call("close_position", symbol=SYMBOL, reason="thesis broken")
    desk.market(600)
    desk.sync()
    events = desk.call("next_event", timeout_seconds=0)["events"]
    closed = [e for e in events if e["key"] == "order:position_closed"]
    assert len(closed) == 1 and "thesis broken" in closed[0]["detail"]
    desk.sync()
    assert desk.call("next_event", timeout_seconds=0)["events"] == []


def test_sync_reports_an_entry_that_did_not_fill():
    desk = AgentDesk()
    desk.market(0)
    desk.call(
        "propose_trade", **proposal(order_type="limit", limit_price="199.5", stop_price="198.5")
    )
    desk.clock.now += timedelta(seconds=200)
    desk.tick()
    desk.sync()
    keys = [e["key"] for e in desk.call("next_event", timeout_seconds=0)["events"]]
    assert keys == ["order:entry_unfilled"]


# --- Management ---


def test_stop_can_only_tighten():
    desk = AgentDesk()
    _enter(desk)
    refused = desk.call("adjust_protection", symbol=SYMBOL, stop_price="198", reason="room")
    assert refused["done"] is False and "stop_widened" in refused["error"]
    done = desk.call("adjust_protection", symbol=SYMBOL, stop_price="199.5", reason="tighten")
    assert done["done"] is True and done["position"]["stop"] == "199.5"
    adjusts = [d for d in _decisions(desk) if d.action is Action.ADJUST]
    assert len(adjusts) == 2
    assert [e.type for e in desk.journal.entries].count("action_failed") == 1


def test_reduce_and_close_are_journaled_and_linked():
    desk = AgentDesk()
    _enter(desk)
    assert desk.call("reduce_position", symbol=SYMBOL, fraction="0.5", reason="took half")["done"]
    desk.market(600)
    assert desk.call("close_position", symbol=SYMBOL, reason="done")["done"]
    desk.market(900)
    assert desk.ledger.open_position(SYMBOL) is None
    actions = [d.action for d in _decisions(desk)]
    assert actions == [Action.ENTER, Action.REDUCE, Action.CLOSE]
    assert sum(isinstance(e, OrderLinked) for e in desk.journal.entries) == 3


def test_management_needs_a_position():
    desk = AgentDesk()
    with pytest.raises(ToolError, match="no open position"):
        desk.call("close_position", symbol=SYMBOL, reason="x")


def test_trailing_stop_and_exit_plan_tools():
    desk = AgentDesk()
    _enter(desk, exit_plan={"breakeven_after_r": "1"})
    answer = desk.call(
        "set_trailing_stop",
        symbol=SYMBOL,
        rule={"kind": "fixed", "distance_bp": "30"},
        reason="momentum",
    )
    assert answer["exit_plan"] == "breakeven at 1R; trail fixed 30 bp"
    answer = desk.call("update_exit_plan", symbol=SYMBOL, plan=None, reason="manual")
    assert answer["exit_plan"] == "remove exit plan"
    assert desk.plans.plan(SYMBOL) is None


def test_add_keeps_the_position_source():
    desk = AgentDesk()
    _enter(desk)
    desk.market(1_000, mark="201.5", bid="201.48", ask="201.52")
    add = proposal(stop_price="200.5", size="0.2")
    for key in ("side", "source"):
        add.pop(key)
    result = desk.call("add_to_position", **add)
    assert result["level"] == "L2"
    decision = _decisions(desk)[-1]
    assert decision.action is Action.ADD
    verdicts = [e for e in desk.journal.entries if isinstance(e, RiskVerdictRecorded)]
    assert verdicts[-1].verdict.adding is True
    assert verdicts[-1].verdict.intent.direction is Direction.LONG


def test_stand_aside_note():
    desk = AgentDesk()
    answer = desk.call(
        "journal",
        note="spread too wide, no edge",
        stand_aside=True,
        symbol=SYMBOL,
        scenarios=[{"name": "chop", "description": "goes nowhere", "probability": "0.7"}],
    )
    decision = _decisions(desk)[0]
    assert answer["recorded"] == decision.decision_id
    assert decision.action is Action.STAND_ASIDE and decision.snapshot is not None


def test_portfolio_and_liveness():
    desk = AgentDesk()
    assert desk.bridge.last_call_at is None
    portfolio = desk.call("get_portfolio")
    assert desk.bridge.last_call_at == NOW
    assert portfolio["trades_today"] == "0/12" and portfolio["l2_trades_today"] == "0/4"
    assert portfolio["risk_budget_full_size"] == {"L0_L1": "3.50", "L2": "1.75"}
    assert desk.call("get_briefing") == {"briefing": "brief"}


def test_describe_tools_has_schemas():
    desk = AgentDesk()
    tools = {t["name"]: t for t in desk.bridge.describe_tools()}
    assert "propose_trade" in tools and "next_event" in tools
    assert "scenarios" in tools["propose_trade"]["input"]["properties"]


def test_liveness_tracks_calls_in_flight():
    desk = AgentDesk()
    seen = []

    async def waiting() -> None:
        task = asyncio.create_task(desk.bridge.call("next_event", {"timeout_seconds": 1}))
        await asyncio.sleep(0)
        seen.append(desk.bridge.calls_in_flight)
        await task

    desk.run(waiting())
    assert seen == [1]
    assert desk.bridge.calls_in_flight == 0
    assert desk.bridge.last_seen_at == NOW


def test_latency_of_each_delivered_event_is_recorded():
    desk = AgentDesk()
    records = []
    desk.bridge._latency_sink = records.append
    desk.bridge.notify(TriggerKind.CHECK_IN, "check_in", "check-in", at=NOW - timedelta(seconds=5))
    desk.call("next_event", timeout_seconds=0)
    assert records == []
    desk.clock.now = NOW + timedelta(seconds=20)
    desk.call("get_portfolio")
    assert len(records) == 1
    record = records[0]
    assert record.key == "check_in" and record.answer_tool == "get_portfolio"
    assert record.pickup_seconds == 5 and record.answer_seconds == 20
    desk.bridge.notify(TriggerKind.CHECK_IN, "check_in", "again")
    desk.call("next_event", timeout_seconds=0)
    desk.bridge.flush_latency()
    assert records[-1].answered_at is None and records[-1].answer_seconds is None
