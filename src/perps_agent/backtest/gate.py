"""The Gate 1 playbook screen (go-no-go.md, ADR-0008) and its records.

For each playbook version, over the backtest window, restricted to the
sessions whose regime (at the window end) matches the playbook's declared
regimes:

* **fires**: the trigger fired at least once. Whether it fired on the setups
  it describes is a review of the listed sample, not automated;
* **expectancy**: net expectancy per trade, after fees and funding, is at
  least ``backtest.min_expectancy_r`` (0);
* **concentration**: no single trade accounts for more than
  ``backtest.max_trade_share`` (25%) of the net PnL;
* **regime filter**: no trade was entered while the regime was outside the
  declared ones (the playbook's regime filter held).

A version passes when all four hold. Trade counts and the breakdown by regime
and symbol are reported, not required: a thin sample is acceptable because
paper trading is the main evidence. Trades in sessions outside the target
regimes are reported separately and do not count.

Each result is written to ``<results_dir>/gate1/<tag>.json``; a later run of the
same version replaces it. :func:`screened_playbooks` reads the passes back for
the session, which treats them like the owner-exempt versions (L0/L1 allowed).
"""

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from perps_agent.backtest.runner import BacktestResult, BacktestTrade, PlaybookTally
from perps_agent.config.models import BacktestConfig
from perps_agent.strategy.playbooks import Playbook

log = logging.getLogger(__name__)

GATE1_DIR = "gate1"
RECORD_VERSION = 1
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Criterion:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Stats:
    trades: int
    wins: int
    net_pnl: Decimal
    net_r: Decimal
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal

    @property
    def expectancy_r(self) -> Decimal | None:
        return self.net_r / self.trades if self.trades else None

    @property
    def win_rate(self) -> Decimal | None:
        return Decimal(self.wins) / self.trades if self.trades else None


def stats(trades: Sequence[BacktestTrade]) -> Stats:
    return Stats(
        trades=len(trades),
        wins=sum(1 for t in trades if t.net_pnl > 0),
        net_pnl=sum((t.net_pnl for t in trades), ZERO),
        net_r=sum((t.net_r for t in trades), ZERO),
        gross_pnl=sum((t.gross_pnl for t in trades), ZERO),
        fees=sum((t.fees for t in trades), ZERO),
        funding=sum((t.funding for t in trades), ZERO),
    )


@dataclass(frozen=True, slots=True)
class Gate1Result:
    tag: str
    playbook_id: str
    passed: bool
    criteria: tuple[Criterion, ...]
    window_start: date
    window_end: date
    run_at: datetime
    sessions: int
    target_sessions: int
    tally: PlaybookTally
    target: Stats
    other: Stats
    # Largest single trade's share of the in-target net PnL (None unless it is positive).
    max_trade_share: Decimal | None
    by_regime: Mapping[str, Stats]
    by_symbol: Mapping[str, Stats]
    exits: Mapping[str, int]
    sample: tuple[BacktestTrade, ...]


def evaluate(
    playbook: Playbook[Any],
    result: BacktestResult,
    config: BacktestConfig,
    run_at: datetime,
) -> Gate1Result:
    trades = [t for t in result.trades if t.playbook == playbook.tag]
    target = [t for t in trades if playbook.applies(t.session_regime)]
    other = [t for t in trades if not playbook.applies(t.session_regime)]
    target_stats = stats(target)
    sessions = len(result.sessions)
    target_sessions = sum(1 for s in result.sessions if playbook.applies(s.regime))

    fires = Criterion(
        "fires",
        bool(target),
        f"{len(target)} trades in {target_sessions} target sessions (of {sessions})",
    )
    expectancy = target_stats.expectancy_r
    expectancy_ok = expectancy is not None and expectancy >= config.min_expectancy_r
    expectancy_c = Criterion(
        "expectancy",
        expectancy_ok,
        "no trades"
        if expectancy is None
        else f"{expectancy:.3f}R per trade net (minimum {config.min_expectancy_r}R)",
    )
    share = None
    if target_stats.net_pnl > 0:
        share = max(t.net_pnl for t in target) / target_stats.net_pnl
        concentration = Criterion(
            "concentration",
            share <= config.max_trade_share,
            f"largest trade {share:.0%} of net PnL (maximum {config.max_trade_share:.0%})",
        )
    else:
        concentration = Criterion(
            "concentration", bool(target), "no net profit to concentrate" if target else "no trades"
        )
    outside = [t for t in trades if not playbook.applies(t.entry_regime)]
    regime_filter = Criterion(
        "regime_filter",
        not outside,
        "every entry inside the declared regimes"
        if not outside
        else f"{len(outside)} entries outside the declared regimes",
    )
    criteria = (fires, expectancy_c, concentration, regime_filter)

    by_regime: dict[str, list[BacktestTrade]] = {}
    by_symbol: dict[str, list[BacktestTrade]] = {}
    exits: dict[str, int] = {}
    for trade in target:
        by_regime.setdefault(regime_key(trade), []).append(trade)
        by_symbol.setdefault(trade.symbol, []).append(trade)
        exits[str(trade.exit_reason)] = exits.get(str(trade.exit_reason), 0) + 1
    return Gate1Result(
        tag=playbook.tag,
        playbook_id=playbook.id,
        passed=all(c.passed for c in criteria),
        criteria=criteria,
        window_start=result.start,
        window_end=result.end,
        run_at=run_at,
        sessions=sessions,
        target_sessions=target_sessions,
        tally=result.tallies.get(playbook.tag, PlaybookTally()),
        target=target_stats,
        other=stats(other),
        max_trade_share=share,
        by_regime={k: stats(v) for k, v in sorted(by_regime.items())},
        by_symbol={k: stats(v) for k, v in sorted(by_symbol.items())},
        exits=dict(sorted(exits.items())),
        sample=review_sample(target, config.review_sample),
    )


def regime_key(trade: BacktestTrade) -> str:
    regime = trade.session_regime
    return f"{regime.volatility or 'unknown'}/{regime.structure or 'unknown'}"


def review_sample(trades: Sequence[BacktestTrade], count: int) -> tuple[BacktestTrade, ...]:
    """``count`` trades spread evenly over the window, for the trigger review."""
    if count <= 0 or not trades:
        return ()
    ordered = sorted(trades, key=lambda t: t.signal_at)
    if len(ordered) <= count:
        return tuple(ordered)
    step = len(ordered) / count
    return tuple(ordered[int(i * step)] for i in range(count))


# --- Records ---


def _decimal(value: Decimal | None, places: int = 4) -> str | None:
    return None if value is None else str(round(value, places))


def stats_json(value: Stats) -> dict[str, Any]:
    return {
        "trades": value.trades,
        "wins": value.wins,
        "win_rate": _decimal(value.win_rate),
        "net_pnl": _decimal(value.net_pnl),
        "expectancy_r": _decimal(value.expectancy_r),
        "net_r": _decimal(value.net_r),
        "gross_pnl": _decimal(value.gross_pnl),
        "fees": _decimal(value.fees),
        "funding": _decimal(value.funding),
    }


def trade_json(trade: BacktestTrade) -> dict[str, Any]:
    return {
        "playbook": trade.playbook,
        "symbol": trade.symbol,
        "day": trade.day.isoformat(),
        "direction": str(trade.direction),
        "variant": trade.variant,
        "conviction": trade.conviction,
        "reasons": list(trade.reasons),
        "signal_at": trade.signal_at.isoformat(),
        "entry_at": trade.entry_at.isoformat(),
        "entry_price": str(trade.entry_price),
        "marketable": trade.marketable,
        "stop_price": str(trade.stop_price),
        "target_price": None if trade.target_price is None else str(trade.target_price),
        "time_stop": trade.time_stop.isoformat(),
        "quantity": _decimal(trade.quantity, 6),
        "risk": _decimal(trade.risk),
        "exit_at": trade.exit_at.isoformat(),
        "exit_price": _decimal(trade.exit_price, 6),
        "exit_reason": str(trade.exit_reason),
        "gross_pnl": _decimal(trade.gross_pnl),
        "fees": _decimal(trade.fees),
        "funding": _decimal(trade.funding),
        "net_pnl": _decimal(trade.net_pnl),
        "net_r": _decimal(trade.net_r, 3),
        "entry_regime": list(trade.entry_regime.labels),
        "session_regime": list(trade.session_regime.labels),
    }


def result_json(result: Gate1Result) -> dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "tag": result.tag,
        "playbook": result.playbook_id,
        "verdict": "pass" if result.passed else "fail",
        "criteria": [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in result.criteria
        ],
        "review": "pending",
        "window": {"start": result.window_start.isoformat(), "end": result.window_end.isoformat()},
        "run_at": result.run_at.isoformat(),
        "sessions": result.sessions,
        "target_sessions": result.target_sessions,
        "signals": result.tally.signals,
        "unfilled": result.tally.unfilled,
        "target": stats_json(result.target),
        "other_sessions": stats_json(result.other),
        "max_trade_share": _decimal(result.max_trade_share),
        "by_regime": {k: stats_json(v) for k, v in result.by_regime.items()},
        "by_symbol": {k: stats_json(v) for k, v in result.by_symbol.items()},
        "exits": dict(result.exits),
        "sample": [trade_json(t) for t in result.sample],
    }


def write_records(results_dir: Path, results: Iterable[Gate1Result]) -> list[Path]:
    directory = results_dir / GATE1_DIR
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for result in results:
        path = directory / f"{result.tag}.json"
        path.write_text(json.dumps(result_json(result), indent=1) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def read_records(results_dir: Path) -> dict[str, dict[str, Any]]:
    """Every readable Gate 1 record by tag."""
    records: dict[str, dict[str, Any]] = {}
    directory = results_dir / GATE1_DIR
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            log.warning("unreadable gate 1 record", extra={"path": str(path), "error": str(error)})
            continue
        if isinstance(record, dict) and isinstance(record.get("tag"), str):
            records[record["tag"]] = record
    return records


def screened_playbooks(results_dir: Path, exempt: Iterable[str] = ()) -> tuple[str, ...]:
    """Versions that may trade as L0/L1: the owner-exempt ones and every Gate 1 pass."""
    passed = {tag for tag, r in read_records(results_dir).items() if r.get("verdict") == "pass"}
    return tuple(sorted(passed | set(exempt)))
