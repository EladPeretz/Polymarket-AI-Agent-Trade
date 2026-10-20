"""Command-line entry point: ``perps-agent``."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from perps_agent import __version__
from perps_agent.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    Mode,
    load_config,
    load_secrets,
    readiness_problems,
)
from perps_agent.log import configure_logging
from perps_agent.market import MarketFeed, MarketSnapshot, Recorder
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

    args = parser.parse_args(argv)
    if args.command == "config-check":
        return _config_check(args.config, args.mode, strict=args.strict)
    if args.command == "record":
        return _record(args.config, args.seconds)
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


if __name__ == "__main__":
    sys.exit(main())
