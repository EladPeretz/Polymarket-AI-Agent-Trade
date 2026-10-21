from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from perps_agent.config.models import GuardianConfig
from perps_agent.execution import ExecutionError, SimBackend
from perps_agent.guardian import (
    HOLD_AGENT,
    HOLD_KILL_SWITCH,
    HOLD_MISMATCH,
    Guardian,
    Incident,
    IncidentKind,
    IncidentLog,
    JsonlIncidentSink,
    Severity,
    load_incidents,
)
from perps_agent.market import MarketSnapshot
from perps_agent.risk import AutonomyLevel
from perps_agent.venue import OrderRequest, OrderState, RequestRejected
from perps_agent.venue.models import Position
from tests.unit.execution_factories import Desk
from tests.unit.risk_factories import NOW, intent, snapshot
from tests.unit.strategy_factories import WINDOW

D = Decimal
SYMBOL = "NVDA-USD"


@dataclass
class FakeAgent:
    last_seen_at: datetime | None = None
    calls_in_flight: int = 0


class FlakyBackend(SimBackend):
    """Refuses exits while ``fail_exits`` is set; can report an extra venue position."""

    fail_exits = False
    extra: tuple[Position, ...] = ()

    async def place_order(self, request: OrderRequest) -> OrderState:
        if self.fail_exits and request.reduce_only:
            raise RequestRejected("venue busy", status=503)
        return await super().place_order(request)

    async def fetch_positions(self) -> tuple[Position, ...]:
        return (*await super().fetch_positions(), *self.extra)


class Setup:
    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.desk = Desk()
        self.backend = FlakyBackend(self.desk.sim)
        self.desk.execution._backend = self.backend
        self.stale = False
        self.agent = FakeAgent(last_seen_at=NOW)
        self.alerts: list[Incident] = []
        self.log = IncidentLog(alert=self.alerts.append, clock=self.desk.clock)
        self.kill_file = tmp_path / "KILL_SWITCH"
        self.guardian = Guardian(
            config=GuardianConfig(kill_switch_file=str(self.kill_file), **config),
            window=WINDOW,
            execution=self.desk.execution,
            incidents=self.log,
            symbols=[SYMBOL],
            snapshot=self.snapshot,
            agent=self.agent,
            clock=self.desk.clock,
        )

    def snapshot(self, symbol: str) -> MarketSnapshot:
        d = self.desk
        return snapshot(bid=d.bid, ask=d.ask, mark=d.mark, taken_at=d.clock.now, stale=self.stale)

    def at(self, when: datetime) -> None:
        self.desk.clock.now = when
        self.desk.run(self.guardian.check(when))
        self.desk.drain()

    def open_position(self) -> None:
        self.desk.market(0)
        self.desk.enter(intent(entry=None, stop="199", level=AutonomyLevel.L2))
        self.desk.market(300)
        assert self.desk.ledger.open_positions

    def book_at(self, when: datetime) -> None:
        self.desk.market((when - NOW) // timedelta(milliseconds=1))

    def kinds(self) -> list[IncidentKind]:
        return [i.kind for i in self.log.incidents]


def test_nothing_happens_inside_the_window(tmp_path):
    s = Setup(tmp_path)
    s.open_position()
    s.at(NOW + timedelta(minutes=1))
    assert s.log.incidents == ()
    assert s.desk.ledger.open_positions
    assert not s.guardian.session_over(NOW)


def test_the_window_close_flattens_the_book(tmp_path):
    s = Setup(tmp_path)
    s.open_position()
    s.at(WINDOW.end)
    s.book_at(WINDOW.end + timedelta(seconds=1))
    assert s.guardian.flat
    assert s.desk.ledger.positions[0].closed_at is not None
    assert s.kinds() == [IncidentKind.WINDOW_CLOSE]
    assert s.log.incidents[0].severity is Severity.INFO
    assert s.alerts == []
    assert s.guardian.session_over(WINDOW.end + timedelta(seconds=1))


def test_still_open_at_the_hard_deadline_is_critical_once(tmp_path):
    s = Setup(tmp_path)
    s.open_position()
    s.backend.fail_exits = True
    s.at(WINDOW.end)
    s.at(WINDOW.end + timedelta(seconds=1))
    assert s.kinds() == [IncidentKind.WINDOW_CLOSE, IncidentKind.CLOSE_FAILED]
    assert not s.guardian.session_over(WINDOW.end + timedelta(seconds=1))
    s.at(WINDOW.hard_flat_deadline)
    s.at(WINDOW.hard_flat_deadline + timedelta(seconds=1))
    assert s.kinds().count(IncidentKind.NOT_FLAT) == 1
    critical = [i for i in s.alerts if i.severity is Severity.CRITICAL]
    assert critical and "NVDA-USD long" in critical[0].detail
    assert s.guardian.session_over(WINDOW.hard_flat_deadline)


def test_close_retries_until_the_venue_accepts(tmp_path):
    s = Setup(tmp_path)
    s.open_position()
    s.backend.fail_exits = True
    s.at(WINDOW.end)
    s.backend.fail_exits = False
    s.at(WINDOW.end + timedelta(seconds=1))
    s.book_at(WINDOW.end + timedelta(seconds=2))
    assert s.guardian.flat


def test_kill_switch_file_closes_everything_and_holds_entries(tmp_path):
    s = Setup(tmp_path)
    s.open_position()
    s.kill_file.write_text("owner: stop for today")
    s.at(NOW + timedelta(seconds=1))
    s.book_at(NOW + timedelta(seconds=2))
    assert s.guardian.flat
    assert "owner: stop for today" in (s.guardian.killed or "")
    assert HOLD_KILL_SWITCH in s.desk.execution.holds
    assert s.kinds() == [IncidentKind.KILL_SWITCH]
    assert s.alerts[0].severity is Severity.CRITICAL
    # Latched: removing the file does not resume trading this session.
    s.kill_file.unlink()
    s.at(NOW + timedelta(seconds=3))
    verdict = s.desk.evaluate(intent(entry=None, stop="199", level=AutonomyLevel.L2))
    with pytest.raises(ExecutionError, match="kill switch"):
        s.desk.run(s.desk.execution.enter(verdict))


def test_kill_switch_config_flag(tmp_path):
    s = Setup(tmp_path, kill_switch=True)
    s.at(NOW)
    assert s.guardian.killed == "guardian.kill_switch is set"
    assert s.desk.execution.entry_block is not None


def test_a_silent_agent_holds_entries_until_it_calls_again(tmp_path):
    s = Setup(tmp_path, agent_timeout_seconds=600)
    s.at(NOW + timedelta(seconds=600))
    assert HOLD_AGENT not in s.desk.execution.holds
    s.at(NOW + timedelta(seconds=601))
    assert HOLD_AGENT in s.desk.execution.holds
    assert s.kinds() == [IncidentKind.AGENT_UNRESPONSIVE]
    assert s.alerts[0].severity is Severity.WARNING
    s.at(NOW + timedelta(seconds=700))
    assert s.kinds() == [IncidentKind.AGENT_UNRESPONSIVE]
    s.agent.last_seen_at = NOW + timedelta(seconds=710)
    s.at(NOW + timedelta(seconds=711))
    assert HOLD_AGENT not in s.desk.execution.holds
    assert s.kinds()[-1] is IncidentKind.AGENT_RESUMED
    assert "after 12 min" in s.log.incidents[-1].detail


def test_a_waiting_call_counts_as_alive(tmp_path):
    s = Setup(tmp_path)
    s.agent.calls_in_flight = 1
    s.at(NOW + timedelta(hours=1))
    assert s.log.incidents == ()


def test_liveness_is_not_checked_after_the_entry_cutoff(tmp_path):
    s = Setup(tmp_path)
    s.at(WINDOW.entry_cutoff + timedelta(minutes=1))
    assert s.log.incidents == ()


def test_an_agent_that_never_called_is_timed_from_the_window_start(tmp_path):
    s = Setup(tmp_path)
    s.agent.last_seen_at = None
    s.desk.clock.now = WINDOW.start - timedelta(minutes=20)
    guardian = Guardian(
        config=GuardianConfig(kill_switch_file=str(s.kill_file)),
        window=WINDOW,
        execution=s.desk.execution,
        incidents=s.log,
        symbols=[SYMBOL],
        snapshot=s.snapshot,
        agent=s.agent,
        clock=s.desk.clock,
    )
    s.desk.run(guardian.check(WINDOW.start + timedelta(seconds=599)))
    assert s.log.incidents == ()
    s.desk.run(guardian.check(WINDOW.start + timedelta(seconds=601)))
    assert s.kinds() == [IncidentKind.AGENT_UNRESPONSIVE]


def test_stale_data_is_an_incident_and_recovery_is_noted(tmp_path):
    s = Setup(tmp_path, stale_incident_seconds=30)
    s.stale = True
    s.at(NOW)
    s.at(NOW + timedelta(seconds=29))
    assert s.log.incidents == ()
    s.at(NOW + timedelta(seconds=30))
    s.at(NOW + timedelta(seconds=40))
    assert s.kinds() == [IncidentKind.STALE_DATA]
    assert s.log.incidents[0].symbol == SYMBOL
    s.stale = False
    s.at(NOW + timedelta(seconds=45))
    assert s.kinds() == [IncidentKind.STALE_DATA, IncidentKind.DATA_RECOVERED]
    assert "45s" in s.log.incidents[-1].detail


def test_short_staleness_is_not_an_incident(tmp_path):
    s = Setup(tmp_path, stale_incident_seconds=30)
    s.stale = True
    s.at(NOW)
    s.stale = False
    s.at(NOW + timedelta(seconds=5))
    assert s.log.incidents == ()


def test_a_mismatch_seen_twice_holds_entries(tmp_path):
    s = Setup(tmp_path, reconcile_interval_seconds=60)
    s.backend.extra = (
        Position(instrument_id=19, quantity=D(1), entry_price=D(200), leverage=D(1), isolated=True),
    )
    s.at(NOW)
    assert HOLD_MISMATCH not in s.desk.execution.holds
    s.at(NOW + timedelta(seconds=30))  # not due yet
    s.at(NOW + timedelta(seconds=60))
    assert HOLD_MISMATCH in s.desk.execution.holds
    assert s.kinds() == [IncidentKind.LEDGER_MISMATCH]


def test_a_mismatch_that_clears_is_ignored(tmp_path):
    s = Setup(tmp_path, reconcile_interval_seconds=60)
    s.backend.extra = (
        Position(instrument_id=19, quantity=D(1), entry_price=D(200), leverage=D(1), isolated=True),
    )
    s.at(NOW)
    s.backend.extra = ()
    s.at(NOW + timedelta(seconds=60))
    s.backend.extra = (
        Position(instrument_id=19, quantity=D(1), entry_price=D(200), leverage=D(1), isolated=True),
    )
    s.at(NOW + timedelta(seconds=120))
    assert s.log.incidents == ()


def test_execution_incidents_are_recorded_once(tmp_path):
    s = Setup(tmp_path)
    s.desk.execution._block_entries("liquidation on instrument 19")
    s.at(NOW)
    s.at(NOW + timedelta(seconds=1))
    assert s.kinds() == [IncidentKind.EXECUTION]


def test_engine_errors_are_deduplicated(tmp_path):
    s = Setup(tmp_path)
    s.guardian.record_error("engine step", ValueError("boom"))
    s.guardian.record_error("engine step", ValueError("boom"))
    s.guardian.record_error("engine step", ValueError("other"))
    assert s.kinds() == [IncidentKind.ENGINE_ERROR, IncidentKind.ENGINE_ERROR]


def test_holds_are_independent_of_incidents(tmp_path):
    s = Setup(tmp_path)
    execution = s.desk.execution
    execution.hold_entries("a", "first")
    execution.hold_entries("b", "second")
    assert execution.entry_block == "first"
    execution.release_entries("a")
    assert execution.entry_block == "second"
    execution._block_entries("incident")
    execution.release_entries("b")
    assert execution.entry_block == "incident" and execution.incident == "incident"
    execution.clear_entry_block()
    assert execution.entry_block is None


def test_incidents_round_trip_through_jsonl(tmp_path):
    path = tmp_path / "incidents.jsonl"
    sink = JsonlIncidentSink(path)
    log = IncidentLog(sink=sink, alert=lambda _: None, clock=lambda: NOW)
    log.record(IncidentKind.STALE_DATA, Severity.WARNING, "stale", symbol=SYMBOL)
    log.record(IncidentKind.DATA_RECOVERED, Severity.INFO, "fresh", symbol=SYMBOL)
    sink.close()
    loaded = load_incidents(path)
    assert loaded == list(log.incidents)
    assert [i.sequence for i in loaded] == [1, 2]
