import json
from datetime import date
from decimal import Decimal

from perps_agent.agent import SessionActivity, build_briefing, recent_activity, system_prompt
from perps_agent.app.cli import main
from perps_agent.config import load_config
from perps_agent.journal import JsonlJournalSink, journal_path
from tests.conftest import COMPLETE_RISK, COMPLETE_SIM, REPO_ROOT
from tests.unit.agent_factories import PLAYBOOKS, VWAP_TAG, AgentDesk, proposal
from tests.unit.strategy_factories import CALENDAR, WINDOW

DAY = date(2026, 10, 20)
REPO_CONFIG = load_config(REPO_ROOT / "config" / "default.yaml")


def _briefing(**overrides) -> str:
    fields = {
        "day": DAY,
        "window": WINDOW,
        "playbooks": PLAYBOOKS,
        "screened": (),
        "activity": (),
    }
    fields.update(overrides)
    return build_briefing(REPO_CONFIG, **fields)


def test_system_prompt_carries_the_owner_style_rules():
    prompt = system_prompt()
    for phrase in (
        "at most about once a week",
        "Do not be afraid of full size",
        "Limit orders by default",
        "No pausing after losses",
        "perps-agent agent <tool>",
        '"screened": false',
        "The market knows better",
        "Listen and change your mind",
        "close the position, wait for the close event",
        "Targets the market can reach",
    ):
        assert phrase in prompt


def test_system_prompt_example_is_a_valid_proposal():
    from perps_agent.agent import TradeProposal

    text = system_prompt()
    example = text.split("```json", 1)[1].split("```", 1)[0]
    TradeProposal.model_validate(json.loads(example))


def test_briefing_has_window_symbol_notes_limits_and_gate_status():
    text = _briefing()
    assert "Trading window 09:30–12:30; entries until 12:15" in text
    assert "**MSTR-USD** (L0, L1, L2). Trades as a leveraged bitcoin proxy" in text
    assert "EIA inventory data" in text
    assert "Full size risks 0.7% of equity per trade" in text
    assert "0.7% of equity per trade, whatever the source." in text
    assert "30 entries a day. An entry" in text
    assert "a target, if set, at least 1.3R after costs" in text
    assert "Daily loss limit 3% of start-of-day equity" in text
    assert "Not yet through the Gate 1 backtest screen: vwap_reversion" in text
    assert "marked unscreened" in text
    assert "No earlier sessions on record." in text


def test_briefing_states_a_reduced_discretionary_budget():
    risk = REPO_CONFIG.risk.model_copy(
        update={"l2_risk_fraction": Decimal("0.5"), "max_l2_trades_per_day": 4}
    )
    config = REPO_CONFIG.model_copy(update={"risk": risk})
    text = build_briefing(
        config, day=DAY, window=WINDOW, playbooks=PLAYBOOKS, screened=(), activity=()
    )
    assert "(L0/L1); discretionary (L2) trades use 50% of that." in text
    assert "30 entries a day, of which 4 L2." in text


def test_briefing_lists_screened_playbooks_and_idle_days():
    activity = [
        SessionActivity(date(2026, 10, 15), 2),
        SessionActivity(date(2026, 10, 16), 0),
        SessionActivity(date(2026, 10, 19), None),
    ]
    text = _briefing(screened=(VWAP_TAG,), activity=activity)
    assert f"Screened (may trade as L0/L1): {VWAP_TAG}." in text
    assert "Fri 2026-10-16: 0 entries" in text
    assert "Mon 2026-10-19: no session" in text
    assert "Sessions without a trade: 1 of the last 2." in text


def test_recent_activity_counts_entries_from_journals(tmp_path):
    desk = AgentDesk()
    desk.market(0)
    desk.call("journal", note="looking")
    desk.call("propose_trade", **proposal())
    sink = JsonlJournalSink(journal_path(tmp_path, date(2026, 10, 16)))
    for entry in desk.journal.entries:
        sink(entry)
    sink.close()
    quiet = JsonlJournalSink(journal_path(tmp_path, date(2026, 10, 19)))
    quiet(AgentDesk().journal.start_session(date(2026, 10, 19), REPO_CONFIG.mode, ["NVDA-USD"]))
    quiet.close()

    activity = recent_activity(tmp_path, CALENDAR.exchange, DAY)
    assert len(activity) == 5
    by_day = {a.day: a.entries for a in activity}
    # Notes are not entries; a day without a journal had no session.
    assert by_day[date(2026, 10, 16)] == 1
    assert by_day[date(2026, 10, 19)] == 0
    assert by_day[date(2026, 10, 15)] is None


def test_prompt_command_prints_prompt_and_briefing(write_config, capsys):
    path = write_config({"risk": COMPLETE_RISK, "sim": COMPLETE_SIM})
    assert main(["prompt", "--config", str(path), "--date", "2026-10-20"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Role")
    assert "# Briefing for Tuesday 2026-10-20" in out


def test_agent_command_without_an_engine(write_config, tmp_path, capsys):
    path = write_config({"agent": {"socket_path": str(tmp_path / "none.sock")}})
    assert main(["agent", "get_portfolio", "--config", str(path)]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is False and "not reachable" in answer["error"]


def test_agent_command_rejects_bad_json(write_config, capsys):
    path = write_config({})
    assert main(["agent", "propose_trade", "{nope", "--config", str(path)]) == 2
    assert "not valid JSON" in json.loads(capsys.readouterr().out)["error"]


def test_run_refuses_live_mode(write_config, capsys):
    path = write_config({"mode": "live", "risk": COMPLETE_RISK, "sim": COMPLETE_SIM})
    assert main(["run", "--config", str(path)]) == 1
    assert "paper mode only" in capsys.readouterr().err
