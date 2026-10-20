"""Command-line entry point: ``perps-agent``."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from perps_agent import __version__
from perps_agent.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    Mode,
    load_config,
    load_secrets,
    readiness_problems,
)


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

    args = parser.parse_args(argv)
    if args.command == "config-check":
        return _config_check(args.config, args.mode, strict=args.strict)
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


if __name__ == "__main__":
    sys.exit(main())
