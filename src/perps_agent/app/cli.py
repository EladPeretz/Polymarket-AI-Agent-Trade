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
from perps_agent.log import configure_logging
from perps_agent.market import MarketFeed, MarketSnapshot, Recorder
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

    run = commands.add_parser(
        "run",
        help="run today's paper session: engine, simulator and the agent's tool server",
    )
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

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

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args.config)
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


def _run(path: Path) -> int:
    """Run today's paper session. Live mode is not available (no live gateway yet)."""
    try:
        config = load_config(path)
        secrets = load_secrets()
        if config.mode is not Mode.PAPER:
            raise ConfigError(f"run supports paper mode only, not {config.mode.value}")
        require_ready(config, secrets)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    configure_logging(secrets=secrets.secret_values())
    try:
        return asyncio.run(
            run_paper_session(config, playbooks=load_playbooks(), secrets=secrets.secret_values())
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


if __name__ == "__main__":
    sys.exit(main())
