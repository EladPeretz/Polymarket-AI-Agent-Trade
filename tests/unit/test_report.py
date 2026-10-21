from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from perps_agent.agent.latency import AGENT_EVENTS_FILE, JsonlLatencySink
from perps_agent.guardian import INCIDENTS_FILE, IncidentKind, IncidentLog, JsonlIncidentSink
from perps_agent.guardian import Severity as S
from perps_agent.journal import JOURNAL_FILE, JsonlJournalSink, TriggerKind
from perps_agent.ledger import LEDGER_FILE, JsonlLedgerSink
from perps_agent.report import (
    EQUITY_FILE,
    EquitySample,
    JsonlEquitySink,
    build_report,
    max_drawdown,
    render_markdown,
    write_report,
)
from tests.unit.agent_factories import SYMBOL, AgentDesk, proposal
from tests.unit.risk_factories import NOW

D = Decimal


def _session(tmp_path: Path) -> Path:
    """A morning with one closed L2 trade, a rejection, an incident and delivered events."""
    day_dir = tmp_path / "2026-10-20"
    desk = AgentDesk()
    latency = JsonlLatencySink(day_dir / AGENT_EVENTS_FILE)
    desk.bridge._latency_sink = latency
    desk.journal.start_session(NOW.date(), desk.bridge._config.mode, [SYMBOL])
    desk.run(desk.execution.start_day(NOW.date()))
    desk.market(0)
    desk.bridge.notify(TriggerKind.CHECK_IN, "check_in", "check-in", at=NOW)
    desk.call("next_event", timeout_seconds=0)
    desk.clock.now = NOW + timedelta(seconds=3)
    assert desk.call("propose_trade", **proposal())["decision"] == "approved"
    desk.market(3300)
    assert desk.call("propose_trade", **proposal(stop_price="199.9"))["decision"] == "rejected"
    desk.market(60_000, mark="201", bid="200.98", ask="201.02")
    desk.call("close_position", symbol=SYMBOL, reason="target area")
    desk.market(60_300)
    assert desk.ledger.open_position(SYMBOL) is None
    desk.journal.record_outcomes(desk.ledger)
    desk.bridge.flush_latency()
    latency.close()

    ledger = JsonlLedgerSink(day_dir / LEDGER_FILE)
    for event in desk.ledger.events:
        ledger(event)
    ledger.close()
    journal = JsonlJournalSink(day_dir / JOURNAL_FILE)
    for entry in desk.journal.entries:
        journal(entry)
    journal.close()
    incidents = JsonlIncidentSink(day_dir / INCIDENTS_FILE)
    log = IncidentLog(sink=incidents, alert=lambda _: None, clock=lambda: NOW)
    log.record(IncidentKind.STALE_DATA, S.WARNING, "NVDA-USD stale for 31s", symbol=SYMBOL)
    incidents.close()
    equity = JsonlEquitySink(day_dir / EQUITY_FILE)
    for minute, value in enumerate(("500", "499", "497.5", "499.5")):
        equity(EquitySample(at=NOW + timedelta(minutes=minute), equity=D(value), open_positions=1))
    equity.close()
    return day_dir


def test_report_from_a_session(tmp_path):
    day_dir = _session(tmp_path)
    report = build_report(day_dir, clock=lambda: NOW + timedelta(hours=3))
    assert report.day.isoformat() == "2026-10-20"
    assert report.mode == "paper" and report.symbols == (SYMBOL,)
    assert report.start_equity == D(500)
    assert len(report.trades) == 1
    trade = report.trades[0]
    assert trade.level == "L2" and trade.source == "discretionary"
    assert trade.exit_reason == "target area"
    assert trade.net_pnl == trade.gross_pnl - trade.fees + trade.funding
    assert report.net_pnl == trade.net_pnl and report.net_pnl > 0
    assert report.end_equity == D(500) + report.net_pnl
    assert report.return_fraction == report.net_pnl / D(500)
    assert report.max_drawdown == D("2.5")
    assert report.max_drawdown_fraction == D("0.005")
    assert [a.key for a in report.by_level] == ["L2"]
    assert report.by_level[0].wins == 1
    assert report.decisions == {"enter": 2, "close": 1}
    assert report.verdicts == {"approved": 1, "rejected": 1}
    assert "stop_distance" in report.rejections[0].checks
    assert [i.kind for i in report.incidents] == [IncidentKind.STALE_DATA]
    assert report.latency.events == 1 and report.latency.answered == 1
    assert report.latency.answer_median_seconds == 3


def test_markdown_puts_incidents_first_and_hides_scenarios(tmp_path):
    day_dir = _session(tmp_path)
    text = render_markdown(build_report(day_dir))
    assert text.index("## Incidents") < text.index("## Result") < text.index("## Trades")
    assert "NVDA-USD stale for 31s" in text
    assert "| L2 |" in text and "target area" in text
    assert "### Rejections" in text
    # Scenarios and assumptions are for the learning loop only (ADR-0010).
    assert "bounce" not in text and "vwap_holds" not in text


def test_write_report_writes_both_files(tmp_path):
    day_dir = _session(tmp_path)
    report = write_report(day_dir)
    assert (day_dir / "report.md").read_text().startswith("# Daily report 2026-10-20")
    assert '"net_pnl"' in (day_dir / "report.json").read_text()
    assert report.trades


def test_an_empty_day_still_reports(tmp_path):
    day_dir = tmp_path / "2026-10-21"
    day_dir.mkdir()
    report = build_report(day_dir)
    assert report.trades == () and report.start_equity is None
    text = render_markdown(report)
    assert "No trades." in text and "None." in text


def test_max_drawdown_is_peak_to_trough():
    samples = [
        EquitySample(at=NOW + timedelta(minutes=i), equity=D(v), open_positions=0)
        for i, v in enumerate(("505", "495", "510", "500"))
    ]
    assert max_drawdown(D(500), samples) == D(10)
    assert max_drawdown(D(500), []) == D(0)
