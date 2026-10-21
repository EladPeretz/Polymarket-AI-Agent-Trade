"""Checks the daily scheduled session runs before it starts the engine (WP12).

``perps-agent preflight`` answers "should today's paper session start, and will
its results be saved?" in one call, so the scheduled session can stop cleanly
on a non-trading day and fail early, with a reason, instead of an hour later:

* the configuration loads and is ready for paper mode;
* today is a trading day and the window has not already ended;
* today's session has not already run (``perps-agent run`` refuses then);
* the venue answers and every enabled symbol resolves to an instrument;
* the results branch's remote is reachable (warning only: the session still
  trades, and ``publish`` retries; the branch not existing yet is fine, the
  first publish creates it);
* the kill switch is not engaged (warning only: the session then runs, but
  closes everything and takes no entries).

Each check is ``ok``, ``warn`` or ``fail``. A non-trading day or a window that
has ended is ``skip``: nothing to do today.
"""

import asyncio
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from perps_agent.app.session import session_paths
from perps_agent.calendar import CalendarError, TradingCalendar
from perps_agent.config import AppConfig, Mode, Secrets, readiness_problems
from perps_agent.venue import InstrumentRegistry, VenueError, create_market_data


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True, slots=True)
class Preflight:
    checks: tuple[Check, ...]

    @property
    def status(self) -> Status:
        statuses = {c.status for c in self.checks}
        for status in (Status.FAIL, Status.SKIP, Status.WARN):
            if status in statuses:
                return status
        return Status.OK

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "checks": [
                {"name": c.name, "status": c.status.value, "detail": c.detail} for c in self.checks
            ],
        }


# Checks that need the network are injectable, so tests run offline.
VenueCheck = Callable[[AppConfig], Check]
RemoteCheck = Callable[[AppConfig], Check]


def run_preflight(
    config: AppConfig,
    secrets: Secrets,
    now: datetime,
    *,
    venue_check: VenueCheck | None = None,
    remote_check: RemoteCheck | None = None,
) -> Preflight:
    checks = [_config_check(config, secrets)]
    window_check = _window_check(config, now)
    checks.append(window_check)
    if window_check.status in (Status.SKIP, Status.FAIL):
        return Preflight(tuple(checks))
    day = now.astimezone(config.session.zone).date()
    ledger = session_paths(config, day).ledger
    checks.append(
        Check("not_yet_run", Status.FAIL, f"{ledger} exists: today's session already ran")
        if ledger.exists()
        else Check("not_yet_run", Status.OK, f"no session yet for {day.isoformat()}")
    )
    checks.append(_kill_switch_check(config))
    checks.append((venue_check or check_venue)(config))
    checks.append((remote_check or check_results_remote)(config))
    return Preflight(tuple(checks))


def _config_check(config: AppConfig, secrets: Secrets) -> Check:
    if config.mode is not Mode.PAPER:
        return Check("config", Status.FAIL, f"mode is {config.mode.value}; sessions run paper")
    problems = readiness_problems(config, secrets)
    if problems:
        return Check("config", Status.FAIL, "; ".join(problems))
    return Check(
        "config", Status.OK, f"ready for paper mode, {len(config.enabled_symbols)} symbols"
    )


def _window_check(config: AppConfig, now: datetime) -> Check:
    calendar = TradingCalendar(config.session)
    day = now.astimezone(config.session.zone).date()
    try:
        window = calendar.trading_window(day)
        reason = calendar.exchange.closure_reason(day)
    except CalendarError as error:
        return Check("trading_day", Status.FAIL, str(error))
    if window is None:
        return Check("trading_day", Status.SKIP, f"{day.isoformat()}: {reason or 'no window'}")
    zone = config.session.zone
    span = f"{window.start.astimezone(zone):%H:%M}-{window.end.astimezone(zone):%H:%M} {zone.key}"
    if now >= window.end:
        return Check("trading_day", Status.SKIP, f"window {span} has already ended")
    minutes = int((window.start - now).total_seconds() // 60)
    when = f"opens in {minutes} min" if minutes > 0 else "open now"
    early = ", early close" if window.market.early_close else ""
    return Check("trading_day", Status.OK, f"window {span}{early}, {when}")


def _kill_switch_check(config: AppConfig) -> Check:
    switch = Path(config.guardian.kill_switch_file)
    if config.guardian.kill_switch:
        return Check("kill_switch", Status.WARN, "guardian.kill_switch is true: no entries")
    if switch.exists():
        return Check("kill_switch", Status.WARN, f"{switch} exists: no entries this session")
    return Check("kill_switch", Status.OK, "not engaged")


def check_venue(config: AppConfig) -> Check:
    """The venue answers and every enabled symbol is a listed instrument."""

    async def resolve() -> Sequence[str]:
        source = create_market_data(config.venue)
        try:
            registry = InstrumentRegistry(source)
            await registry.refresh()
            return [registry.by_symbol(s).symbol for s in config.enabled_symbols]
        finally:
            await source.aclose()

    try:
        symbols = asyncio.run(resolve())
    except VenueError as error:
        return Check("venue", Status.FAIL, f"{type(error).__name__}: {error}")
    return Check("venue", Status.OK, f"instruments resolved: {', '.join(symbols)}")


def check_results_remote(config: AppConfig, repo: Path | None = None) -> Check:
    """The results branch's remote answers; says whether the branch exists yet."""
    remote, branch = config.results.remote, config.results.branch
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
            cwd=repo,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return Check("results_remote", Status.WARN, f"git ls-remote {remote}: {error}")
    if result.returncode != 0:
        lines = result.stderr.decode(errors="replace").strip().splitlines()
        detail = lines[-1] if lines else f"exit {result.returncode}"
        return Check("results_remote", Status.WARN, f"{remote} not reachable: {detail}")
    if result.stdout.strip():
        return Check("results_remote", Status.OK, f"{remote}/{branch} exists")
    return Check(
        "results_remote",
        Status.OK,
        f"{remote} reachable; {branch} does not exist yet (the first publish creates it)",
    )
