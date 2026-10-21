"""Command-line entry point: ``perps-agent``."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from perps_agent import __version__
from perps_agent.agent import TransportError, system_prompt
from perps_agent.agent import request as agent_request
from perps_agent.app.preflight import Status, run_preflight
from perps_agent.app.rehearsal import RehearsalCalendar, rehearsal_config, rehearsal_window
from perps_agent.app.results import PublishError, fetch_results, local_days, publish_results
from perps_agent.app.session import briefing_for, run_paper_session
from perps_agent.calendar import CalendarError, TradingCalendar
from perps_agent.clock import utc_now
from perps_agent.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    Mode,
    load_config,
    load_secrets,
    readiness_problems,
    require_ready,
)
from perps_agent.guardian import (
    INCIDENTS_FILE,
    Incident,
    IncidentKind,
    Severity,
    append_incident,
)
from perps_agent.log import configure_logging
from perps_agent.market import MarketFeed, MarketSnapshot, Recorder
from perps_agent.report import write_report
from perps_agent.strategy.playbooks import load_playbooks
from perps_agent.venue import InstrumentRegistry, VenueError, create_market_data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perps-agent", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("config-check", help="validate configuration")
    check.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    check.add_argument("--mode", type=Mode, choices=list(Mode))
    check.add_argument(
        "--strict", action="store_true", help="fail unless ready to run in the selected mode"
    )

    record = commands.add_parser(
        "record",
        help="run the market data feed and recorder (read-only, no credentials)",
    )
    record.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    record.add_argument("--seconds", type=float, default=60.0, help="how long to run")

    calendar = commands.add_parser(
        "calendar",
        help="print the trading window for a date (exit 3 on non-trading days)",
    )
    calendar.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    calendar.add_argument(
        "--date",
        type=date.fromisoformat,
        help="exchange date, YYYY-MM-DD (default: today in New York)",
    )

    preflight = commands.add_parser(
        "preflight",
        help="checks before the daily session (exit 0 go, 1 fail, 3 nothing to do today)",
    )
    preflight.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    run = commands.add_parser(
        "run",
        help="run today's paper session: engine, simulator and the agent's tool server",
    )
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run.add_argument(
        "--rehearsal",
        type=int,
        metavar="MINUTES",
        help="rehearse on a window of MINUTES starting now, results under data/rehearsals/",
    )

    agent = commands.add_parser(
        "agent",
        help="call one of the agent's tools on the running engine (prints JSON)",
    )
    agent.add_argument("tool", help="tool name; 'tools' lists them with their input schemas")
    agent.add_argument(
        "args", nargs="?", default=None, help="arguments as one JSON object ('-' reads stdin)"
    )
    agent.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    prompt = commands.add_parser(
        "prompt", help="print the agent's system prompt and today's briefing"
    )
    prompt.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    prompt.add_argument("--date", type=date.fromisoformat, help="exchange date, YYYY-MM-DD")

    report = commands.add_parser(
        "report", help="build a day's report (report.md, report.json) from results/<date>/"
    )
    report.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    report.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD (default: today)")

    publish = commands.add_parser(
        "publish",
        help="commit results/<date>/ to the results branch and push it (ADR-0006)",
    )
    publish.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    publish.add_argument(
        "--date",
        type=date.fromisoformat,
        help="publish one day (default: every local day; days already published add nothing)",
    )

    fetch = commands.add_parser(
        "fetch-results", help="copy recent days from the results branch into results/"
    )
    fetch.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    fetch.add_argument("--days", type=int, default=10, help="newest N days on the branch")

    kill = commands.add_parser(
        "kill",
        help="engage the guardian's kill switch: close everything, no new entries this session",
    )
    kill.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    kill.add_argument("--reason", default="", help="why, recorded in the incident")
    kill.add_argument(
        "--clear", action="store_true", help="remove the kill switch file (for the next session)"
    )

    args = parser.parse_args(argv)
    if args.command == "report":
        return _report(args.config, args.date)
    if args.command == "publish":
        return _publish(args.config, args.date)
    if args.command == "fetch-results":
        return _fetch_results(args.config, args.days)
    if args.command == "kill":
        return _kill(args.config, args.reason, clear=args.clear)
    if args.command == "preflight":
        return _preflight(args.config)
    if args.command == "run":
        return _run(args.config, args.rehearsal)
    if args.command == "agent":
        return _agent(args.config, args.tool, args.args)
    if args.command == "prompt":
        return _prompt(args.config, args.date)
    if args.command == "config-check":
        return _config_check(args.config, args.mode, strict=args.strict)
    if args.command == "record":
        return _record(args.config, args.seconds)
    if args.command == "calendar":
        return _calendar(args.config, args.date)
    return 2  # unreachable: subcommand is required


def _config_check(path: Path, mode: Mode | None, *, strict: bool) -> int:
    try:
        config = load_config(path, mode=mode)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    problems = readiness_problems(config, load_secrets())
    print(json.dumps(config.model_dump(mode="json"), indent=2))
    if problems:
        label = "error" if strict else "warning"
        print(f"\n{label}: not ready for {config.mode.value} mode:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1 if strict else 0
    print(f"\nready for {config.mode.value} mode", file=sys.stderr)
    return 0


def _record(path: Path, seconds: float) -> int:
    """Stream the enabled symbols for ``seconds``, recording them, then print the snapshots."""
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    configure_logging(secrets=load_secrets().secret_values())
    try:
        snapshots = asyncio.run(_run_feed(config, seconds))
    except VenueError as error:
        logging.getLogger(__name__).error("record failed", extra={"error": str(error)})
        return 1
    print(json.dumps([s.model_dump(mode="json") for s in snapshots], indent=2))
    return 0 if not any(s.stale for s in snapshots) else 1


async def _run_feed(config: AppConfig, seconds: float) -> list[MarketSnapshot]:
    source = create_market_data(config.venue)
    try:
        registry = InstrumentRegistry(source)
        await registry.refresh()
        instruments = [registry.by_symbol(symbol) for symbol in config.enabled_symbols]
        recorder = (
            Recorder(
                Path(config.market.recording_dir),
                {i.instrument_id: i.symbol for i in instruments},
            )
            if config.market.record
            else None
        )
        feed = MarketFeed(source, instruments, config.market, recorder=recorder)
        stop = asyncio.Event()
        run = asyncio.create_task(feed.run(stop))
        try:
            await asyncio.wait({run}, timeout=seconds)
            snapshots = [feed.snapshot(i.symbol) for i in instruments]
        finally:
            stop.set()
            await run
            if recorder is not None:
                recorder.close()
        return snapshots
    finally:
        await source.aclose()


# Exit code for a non-trading day, distinct from errors (1) so a scheduled
# session can skip the day without treating it as a failure.
EXIT_NOT_TRADING_DAY = 3


def _calendar(path: Path, day: date | None) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    calendar = TradingCalendar(config.session)
    if day is None:
        day = utc_now().astimezone(config.session.zone).date()
    try:
        window = calendar.trading_window(day)
        reason = calendar.exchange.closure_reason(day)
    except CalendarError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    report: dict[str, object] = {"date": day.isoformat(), "trading_day": reason is None}
    if reason is not None:
        report["closed"] = reason
    if window is not None:
        report.update(
            early_close=window.market.early_close,
            market_open=window.market.open.isoformat(),
            market_close=window.market.close.isoformat(),
            window_start=window.start.isoformat(),
            entry_cutoff=window.entry_cutoff.isoformat(),
            window_end=window.end.isoformat(),
            hard_flat_deadline=window.hard_flat_deadline.isoformat(),
        )
    print(json.dumps(report, indent=2))
    return 0 if window is not None else EXIT_NOT_TRADING_DAY


def _preflight(path: Path) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "checks": [{"name": "config", "status": "fail", "detail": str(error)}],
                },
                indent=2,
            )
        )
        return 1
    result = run_preflight(config, load_secrets(), utc_now())
    print(json.dumps(result.as_dict(), indent=2))
    if result.status is Status.FAIL:
        return 1
    if result.status is Status.SKIP:
        return EXIT_NOT_TRADING_DAY
    return 0


def _run(path: Path, rehearsal_minutes: int | None = None) -> int:
    """Run today's paper session. Live mode is not available (no live gateway yet)."""
    calendar: TradingCalendar | None = None
    try:
        config = load_config(path)
        secrets = load_secrets()
        if config.mode is not Mode.PAPER:
            raise ConfigError(f"run supports paper mode only, not {config.mode.value}")
        require_ready(config, secrets)
        if rehearsal_minutes is not None:
            now = utc_now()
            calendar = RehearsalCalendar(config.session, rehearsal_window(now, rehearsal_minutes))
            config = rehearsal_config(config, now)
    except (ConfigError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    configure_logging(secrets=secrets.secret_values())
    if isinstance(calendar, RehearsalCalendar):
        window = calendar.window
        print(
            json.dumps(
                {
                    "rehearsal": True,
                    "results_dir": config.agent.results_dir,
                    "window_start": window.start.isoformat(),
                    "entry_cutoff": window.entry_cutoff.isoformat(),
                    "window_end": window.end.isoformat(),
                }
            ),
            flush=True,
        )
    try:
        return asyncio.run(
            run_paper_session(
                config,
                playbooks=load_playbooks(),
                secrets=secrets.secret_values(),
                calendar=calendar,
            )
        )
    except (VenueError, FileExistsError) as error:
        logging.getLogger(__name__).error("session failed", extra={"error": str(error)})
        return 1


# Seconds the client waits beyond next_event's own timeout before giving up.
_ANSWER_MARGIN = 30


def _agent(path: Path, tool: str, raw: str | None) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if raw == "-":
        raw = sys.stdin.read()
    try:
        tool_args = json.loads(raw) if raw and raw.strip() else {}
    except ValueError as error:
        print(json.dumps({"ok": False, "error": f"arguments are not valid JSON: {error}"}))
        return 2
    if not isinstance(tool_args, dict):
        print(json.dumps({"ok": False, "error": "arguments must be one JSON object"}))
        return 2
    wait = config.agent.next_event_timeout_seconds
    if tool == "next_event" and isinstance(tool_args.get("timeout_seconds"), int):
        wait = tool_args["timeout_seconds"]
    try:
        answer = asyncio.run(
            agent_request(
                Path(config.agent.socket_path), tool, tool_args, timeout=wait + _ANSWER_MARGIN
            )
        )
    except TransportError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(answer, indent=1, default=str))
    return 0 if answer.get("ok") else 1


def _prompt(path: Path, day: date | None) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    calendar = TradingCalendar(config.session)
    if day is None:
        day = utc_now().astimezone(config.session.zone).date()
    try:
        briefing = briefing_for(config, calendar, day, load_playbooks())
    except CalendarError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(system_prompt())
    print(briefing)
    return 0


def _today(config: AppConfig) -> date:
    return utc_now().astimezone(config.session.zone).date()


def _report(path: Path, day: date | None) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    day = day or _today(config)
    day_dir = Path(config.agent.results_dir) / day.isoformat()
    if not day_dir.is_dir():
        print(f"error: {day_dir} does not exist", file=sys.stderr)
        return 1
    write_report(day_dir)
    print((day_dir / "report.md").read_text(encoding="utf-8"))
    return 0


def _publish(path: Path, day: date | None) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    secrets = load_secrets()
    configure_logging(secrets=secrets.secret_values())
    results = Path(config.agent.results_dir)
    days = [day] if day is not None else local_days(results)
    try:
        published = publish_results(
            results, days, config=config.results, secrets=secrets.secret_values()
        )
    except PublishError as error:
        logging.getLogger(__name__).error("publish failed", extra={"error": str(error)})
        # Recorded with the newest day, so the next publish carries the incident too.
        if days and (results / max(days).isoformat()).is_dir():
            append_incident(
                results / max(days).isoformat() / INCIDENTS_FILE,
                Incident(
                    at=utc_now(),
                    kind=IncidentKind.PUBLISH_FAILED,
                    severity=Severity.CRITICAL,
                    detail=f"results not published to {config.results.branch}: {error}",
                ),
            )
        return 1
    print(
        json.dumps(
            {
                "branch": config.results.branch,
                "days": [d.isoformat() for d in published.days],
                "commit": published.commit,
                "attempts": published.attempts,
            },
            indent=2,
        )
    )
    return 0


def _fetch_results(path: Path, days: int) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    try:
        fetched = fetch_results(Path(config.agent.results_dir), config=config.results, days=days)
    except PublishError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"fetched": [d.isoformat() for d in fetched]}, indent=2))
    return 0


def _kill(path: Path, reason: str, *, clear: bool) -> int:
    try:
        config = load_config(path)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    switch = Path(config.guardian.kill_switch_file)
    if clear:
        switch.unlink(missing_ok=True)
        print(f"kill switch file removed: {switch}")
        return 0
    switch.parent.mkdir(parents=True, exist_ok=True)
    switch.write_text(f"{utc_now().isoformat()} {reason}".strip() + "\n", encoding="utf-8")
    print(f"kill switch engaged: {switch} (a running session acts on its next check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
