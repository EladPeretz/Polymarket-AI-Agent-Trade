"""Daily scheduling (WP12): rehearsal windows and the session preflight."""

import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from perps_agent.app import cli, preflight
from perps_agent.app.cli import main
from perps_agent.app.preflight import Check, Status, check_results_remote, run_preflight
from perps_agent.app.rehearsal import (
    REHEARSAL_DIR,
    RehearsalCalendar,
    rehearsal_config,
    rehearsal_window,
)
from perps_agent.config import Secrets, load_config
from tests.conftest import COMPLETE_RISK, COMPLETE_SIM

# Wednesday 2026-10-21, 04:00 New York (EDT, UTC-4): a trading day, before the window.
EARLY = datetime(2026, 10, 21, 8, 0, 15, tzinfo=UTC)


def _ok(name: str) -> Check:
    return Check(name, Status.OK, "stub")


def _config(write_config, tmp_path: Path, **extra: object):
    path = write_config(
        {
            "risk": COMPLETE_RISK,
            "sim": COMPLETE_SIM,
            "agent": {"results_dir": str(tmp_path / "results")},
            "guardian": {"kill_switch_file": str(tmp_path / "KILL")},
            **extra,
        }
    )
    return load_config(path)


def _preflight(config, now: datetime = EARLY):
    return run_preflight(
        config,
        Secrets(),
        now,
        venue_check=lambda _: _ok("venue"),
        remote_check=lambda _: _ok("results_remote"),
    )


# Rehearsals


def test_rehearsal_window_starts_at_the_next_minute():
    window = rehearsal_window(EARLY, 20)
    assert window.start == datetime(2026, 10, 21, 8, 1, tzinfo=UTC)
    assert window.end == window.start + timedelta(minutes=20)
    assert window.entry_cutoff == window.end - timedelta(minutes=3)
    assert window.hard_flat_deadline == window.end + timedelta(minutes=2)
    assert window.market.open == window.start
    assert window.date == date(2026, 10, 21)


@pytest.mark.parametrize("minutes", [5, 181])
def test_rehearsal_length_is_bounded(minutes):
    with pytest.raises(ValueError):
        rehearsal_window(EARLY, minutes)


def test_rehearsal_calendar_has_only_the_rehearsal_window(write_config, tmp_path):
    config = _config(write_config, tmp_path)
    # A Saturday: the real calendar has no window at all.
    saturday = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
    window = rehearsal_window(saturday, 30)
    calendar = RehearsalCalendar(config.session, window)
    assert calendar.trading_window(window.date) == window
    assert calendar.trading_window(date(2026, 10, 23)) is None
    assert calendar.is_entry_allowed(window.start)
    assert not calendar.is_entry_allowed(window.entry_cutoff)
    assert calendar.must_be_flat(window.end)
    # The exchange calendar (history, prior sessions) is still the real one.
    assert calendar.exchange.trading_day(date(2026, 10, 23)) is not None


def test_rehearsal_results_never_go_to_the_results_directory(write_config, tmp_path):
    config = _config(write_config, tmp_path)
    rehearsal = rehearsal_config(config, EARLY)
    results = Path(rehearsal.agent.results_dir)
    assert results == REHEARSAL_DIR / "20261021T080015Z"
    assert results != Path(config.agent.results_dir)
    assert rehearsal.agent.socket_path == config.agent.socket_path


def test_run_rejects_a_bad_rehearsal_length(write_config, tmp_path, capsys):
    path = write_config({"risk": COMPLETE_RISK, "sim": COMPLETE_SIM})
    assert main(["run", "--config", str(path), "--rehearsal", "2"]) == 1
    assert "rehearsal lasts" in capsys.readouterr().err


# Preflight


def test_preflight_passes_before_the_window(write_config, tmp_path):
    result = _preflight(_config(write_config, tmp_path))
    assert result.status is Status.OK
    names = [c.name for c in result.checks]
    assert names == [
        "config",
        "trading_day",
        "not_yet_run",
        "kill_switch",
        "venue",
        "results_remote",
    ]
    assert "opens in 329 min" in result.checks[1].detail


def test_preflight_skips_a_holiday(write_config, tmp_path):
    thanksgiving = datetime(2026, 11, 26, 13, 10, tzinfo=UTC)
    result = _preflight(_config(write_config, tmp_path), thanksgiving)
    assert result.status is Status.SKIP
    assert [c.name for c in result.checks] == ["config", "trading_day"]


def test_preflight_skips_once_the_window_has_ended(write_config, tmp_path):
    late = datetime(2026, 10, 21, 16, 30, tzinfo=UTC)  # 12:30 New York
    result = _preflight(_config(write_config, tmp_path), late)
    assert result.status is Status.SKIP
    assert "already ended" in result.checks[1].detail


def test_preflight_fails_when_config_is_not_ready(write_config, tmp_path):
    config = load_config(write_config({}))
    result = _preflight(config)
    assert result.status is Status.FAIL
    assert result.checks[0].status is Status.FAIL


def test_preflight_fails_when_the_session_already_ran(write_config, tmp_path):
    config = _config(write_config, tmp_path)
    day = tmp_path / "results" / "2026-10-21"
    day.mkdir(parents=True)
    (day / "ledger.jsonl").write_text("")
    result = _preflight(config)
    assert result.status is Status.FAIL
    assert "already ran" in next(c for c in result.checks if c.name == "not_yet_run").detail


def test_preflight_warns_about_the_kill_switch(write_config, tmp_path):
    config = _config(write_config, tmp_path)
    (tmp_path / "KILL").write_text("stop\n")
    result = _preflight(config)
    assert result.status is Status.WARN


def test_a_failure_outranks_a_warning(write_config, tmp_path):
    config = _config(write_config, tmp_path)
    (tmp_path / "KILL").write_text("stop\n")
    result = run_preflight(
        config,
        Secrets(),
        EARLY,
        venue_check=lambda _: Check("venue", Status.FAIL, "down"),
        remote_check=lambda _: _ok("results_remote"),
    )
    assert result.status is Status.FAIL
    assert result.as_dict()["status"] == "fail"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_results_remote_reports_whether_the_branch_exists(write_config, tmp_path):
    config = _config(write_config, tmp_path)
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(work, "init", "-q")
    _git(work, "remote", "add", "origin", str(remote))

    missing = check_results_remote(config, work)
    assert missing.status is Status.OK
    assert "does not exist yet" in missing.detail

    _git(
        work,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/paper-results")
    assert check_results_remote(config, work).detail == "origin/paper-results exists"


def test_an_unreachable_results_remote_is_a_warning(write_config, tmp_path):
    config = _config(write_config, tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "remote", "add", "origin", str(tmp_path / "missing.git"))
    assert check_results_remote(config, work).status is Status.WARN


def test_cli_preflight_exit_codes(write_config, tmp_path, monkeypatch, capsys):
    path = write_config(
        {
            "risk": COMPLETE_RISK,
            "sim": COMPLETE_SIM,
            "agent": {"results_dir": str(tmp_path / "results")},
        }
    )
    monkeypatch.setattr(preflight, "check_venue", lambda _: _ok("venue"))
    monkeypatch.setattr(preflight, "check_results_remote", lambda _: _ok("results_remote"))

    monkeypatch.setattr(cli, "utc_now", lambda: EARLY)
    assert main(["preflight", "--config", str(path)]) == 0

    monkeypatch.setattr(cli, "utc_now", lambda: datetime(2026, 10, 24, 13, 10, tzinfo=UTC))
    assert main(["preflight", "--config", str(path)]) == 3

    not_ready = write_config({})
    assert main(["preflight", "--config", str(not_ready)]) == 1
    assert '"status": "fail"' in capsys.readouterr().out
