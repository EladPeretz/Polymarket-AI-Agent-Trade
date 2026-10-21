"""The guardian (overview §3.10): an independent, deterministic session supervisor.

:meth:`Guardian.check` runs on every engine step, separately from the engine's
own work, so a failure in one does not stop the other. It has no LLM
dependency and nothing the agent can change (ADR-0010).

* **Window close.** From the window end it cancels every working order and
  closes every position, retrying on each check until the hard flat deadline.
  Still not flat then: a critical incident, and the session ends.
* **Kill switch.** ``guardian.kill_switch`` in the config, or the kill switch
  file (``perps-agent kill``), closes everything and holds new entries. It is
  latched for the rest of the session.
* **Agent liveness.** While entries are allowed, new entries are held once the
  agent has made no tool call for ``agent_timeout_seconds`` (a call in
  progress counts as alive). Positions stay protected by their stops. The hold
  is released when the agent calls again.
* **Stale data.** A symbol stale for ``stale_incident_seconds`` inside the
  window is an incident. The risk engine already refuses entries on stale data.
* **Reconciliation.** Ledger and venue positions are compared every
  ``reconcile_interval_seconds``; a difference on two checks in a row holds new
  entries for the rest of the session.
* **Execution incidents** (an unbooked venue report, a liquidation) and engine
  errors (:meth:`Guardian.record_error`) are recorded as incidents.

The guardian never weakens a risk rule: it only closes, cancels and holds.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from perps_agent.calendar import TradingWindow
from perps_agent.clock import to_utc, utc_now
from perps_agent.config.models import GuardianConfig
from perps_agent.execution import ExecutionEngine, ExecutionError
from perps_agent.guardian.incidents import IncidentKind, IncidentLog, Severity
from perps_agent.market import MarketSnapshot
from perps_agent.venue import VenueError

log = logging.getLogger(__name__)

# Execution hold keys.
HOLD_KILL_SWITCH = "guardian:kill_switch"
HOLD_AGENT = "guardian:agent_liveness"
HOLD_MISMATCH = "guardian:ledger_mismatch"

WINDOW_CLOSE_REASON = "guardian: window close"
KILL_SWITCH_REASON = "guardian: kill switch"


class AgentActivity(Protocol):
    """What the guardian reads from the agent bridge."""

    @property
    def last_seen_at(self) -> datetime | None: ...

    @property
    def calls_in_flight(self) -> int: ...


class Guardian:
    def __init__(
        self,
        *,
        config: GuardianConfig,
        window: TradingWindow,
        execution: ExecutionEngine,
        incidents: IncidentLog,
        symbols: Sequence[str],
        snapshot: Callable[[str], MarketSnapshot],
        agent: AgentActivity | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self.window = window
        self._execution = execution
        self.incidents = incidents
        self._symbols = tuple(symbols)
        self._snapshot = snapshot
        self._agent = agent
        self._started_at = to_utc(clock())
        self._kill_file = Path(config.kill_switch_file)
        self.killed: str | None = None
        self._closing_since: datetime | None = None
        self._close_failure: str | None = None
        self._deadline_reported = False
        self._agent_idle_since: datetime | None = None
        self._stale_since: dict[str, datetime] = {}
        self._stale_reported: set[str] = set()
        self._next_reconcile = self._started_at
        self._mismatch_seen = False
        self._incident_seen: str | None = None
        self._last_error: str | None = None

    # --- Checks ---

    async def check(self, now: datetime) -> None:
        now = to_utc(now)
        self._execution_incident(now)
        await self._kill_switch(now)
        self._liveness(now)
        self._staleness(now)
        await self._reconcile(now)
        await self._window_close(now)

    def session_over(self, now: datetime) -> bool:
        """The window has ended and the book is flat, or the hard deadline has passed."""
        now = to_utc(now)
        if now >= self.window.hard_flat_deadline:
            return True
        return now >= self.window.end and self.flat

    @property
    def flat(self) -> bool:
        ledger = self._execution.ledger
        return not ledger.open_positions and not ledger.live_orders()

    def record_error(self, where: str, error: BaseException, now: datetime | None = None) -> None:
        """An engine failure; repeated identical errors are recorded once."""
        message = f"{where}: {type(error).__name__}: {error}"
        if message == self._last_error:
            return
        self._last_error = message
        self.incidents.record(IncidentKind.ENGINE_ERROR, Severity.CRITICAL, message, at=now)

    # --- Window close ---

    async def _window_close(self, now: datetime) -> None:
        if now < self.window.end or self.flat:
            return
        if self._closing_since is None:
            self._closing_since = now
            self.incidents.record(
                IncidentKind.WINDOW_CLOSE,
                Severity.INFO,
                f"window closed with {self._book()}: closing everything",
                at=now,
            )
        await self._close_all(now, WINDOW_CLOSE_REASON)
        if now >= self.window.hard_flat_deadline and not self.flat and not self._deadline_reported:
            self._deadline_reported = True
            self.incidents.record(
                IncidentKind.NOT_FLAT,
                Severity.CRITICAL,
                f"still not flat at the hard deadline: {self._book()}",
                at=now,
            )

    async def _close_all(self, now: datetime, reason: str) -> None:
        try:
            await self._execution.close_all(reason)
        except (ExecutionError, VenueError) as error:
            # Retried on the next check; reported once per distinct failure.
            message = f"{reason}: close failed: {error}"
            if message != self._close_failure:
                self._close_failure = message
                self.incidents.record(IncidentKind.CLOSE_FAILED, Severity.WARNING, message, at=now)

    def _book(self) -> str:
        ledger = self._execution.ledger
        positions = [f"{p.symbol} {p.direction} {p.quantity}" for p in ledger.open_positions]
        orders = len(ledger.live_orders())
        parts = []
        if positions:
            parts.append("positions " + ", ".join(positions))
        if orders:
            parts.append(f"{orders} working order(s)")
        return "; ".join(parts) or "a flat book"

    # --- Kill switch ---

    async def _kill_switch(self, now: datetime) -> None:
        if self.killed is None:
            reason = self._kill_requested()
            if reason is None:
                return
            self.killed = reason
            self._execution.hold_entries(HOLD_KILL_SWITCH, f"kill switch: {reason}")
            self.incidents.record(
                IncidentKind.KILL_SWITCH,
                Severity.CRITICAL,
                f"kill switch engaged ({reason}): closing everything, no new entries",
                at=now,
            )
        if not self.flat:
            await self._close_all(now, KILL_SWITCH_REASON)

    def _kill_requested(self) -> str | None:
        if self._config.kill_switch:
            return "guardian.kill_switch is set"
        try:
            if not self._kill_file.exists():
                return None
            note = self._kill_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            return f"kill switch file {self._kill_file} unreadable: {error}"
        return f"{self._kill_file}" + (f": {note[:200]}" if note else "")

    # --- Agent liveness ---

    def _liveness(self, now: datetime) -> None:
        agent = self._agent
        if agent is None or not (self.window.start <= now < self.window.entry_cutoff):
            return
        timeout = timedelta(seconds=self._config.agent_timeout_seconds)
        if agent.calls_in_flight > 0:
            idle = timedelta(0)
        else:
            seen = [t for t in (agent.last_seen_at, self.window.start, self._started_at) if t]
            idle = now - max(seen)
        if idle > timeout and self._agent_idle_since is None:
            self._agent_idle_since = now - idle
            minutes = idle.total_seconds() / 60
            self._execution.hold_entries(
                HOLD_AGENT, f"agent unresponsive for {minutes:.0f} min; entries held"
            )
            self.incidents.record(
                IncidentKind.AGENT_UNRESPONSIVE,
                Severity.WARNING,
                f"no agent tool call for {minutes:.0f} min: new entries held, "
                "positions stay protected by their stops",
                at=now,
            )
        elif idle <= timeout and self._agent_idle_since is not None:
            away = (now - self._agent_idle_since).total_seconds() / 60
            self._agent_idle_since = None
            self._execution.release_entries(HOLD_AGENT)
            self.incidents.record(
                IncidentKind.AGENT_RESUMED,
                Severity.INFO,
                f"agent active again after {away:.0f} min: entries released",
                at=now,
            )

    # --- Stale data ---

    def _staleness(self, now: datetime) -> None:
        if not self.window.contains(now):
            return
        threshold = timedelta(seconds=self._config.stale_incident_seconds)
        for symbol in self._symbols:
            if self._snapshot(symbol).stale:
                since = self._stale_since.setdefault(symbol, now)
                if now - since >= threshold and symbol not in self._stale_reported:
                    self._stale_reported.add(symbol)
                    self.incidents.record(
                        IncidentKind.STALE_DATA,
                        Severity.WARNING,
                        f"{symbol} market data stale for {(now - since).total_seconds():.0f}s",
                        symbol=symbol,
                        at=now,
                    )
                continue
            if symbol not in self._stale_since:
                continue
            since = self._stale_since.pop(symbol)
            if symbol in self._stale_reported:
                self._stale_reported.discard(symbol)
                self.incidents.record(
                    IncidentKind.DATA_RECOVERED,
                    Severity.INFO,
                    f"{symbol} market data fresh again after {(now - since).total_seconds():.0f}s",
                    symbol=symbol,
                    at=now,
                )

    # --- Reconciliation and execution incidents ---

    async def _reconcile(self, now: datetime) -> None:
        if now < self._next_reconcile or HOLD_MISMATCH in self._execution.holds:
            return
        self._next_reconcile = now + timedelta(seconds=self._config.reconcile_interval_seconds)
        try:
            problems = await self._execution.mismatches()
        except VenueError as error:
            log.warning("reconciliation skipped", extra={"error": str(error)})
            return
        if not problems:
            self._mismatch_seen = False
            return
        if not self._mismatch_seen:
            # Seen once: check again on the next pass before acting.
            self._mismatch_seen = True
            return
        detail = "; ".join(problems)
        self._execution.hold_entries(HOLD_MISMATCH, f"ledger and venue disagree: {detail}")
        self.incidents.record(
            IncidentKind.LEDGER_MISMATCH,
            Severity.CRITICAL,
            f"ledger and venue positions disagree: {detail}; new entries held",
            at=now,
        )

    def _execution_incident(self, now: datetime) -> None:
        incident = self._execution.incident
        if incident is not None and incident != self._incident_seen:
            self.incidents.record(
                IncidentKind.EXECUTION,
                Severity.CRITICAL,
                f"execution blocked new entries: {incident}",
                at=now,
            )
        self._incident_seen = incident
