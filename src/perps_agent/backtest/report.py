"""The backtest report: every playbook version's Gate 1 result, as Markdown and JSON."""

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from perps_agent.backtest.gate import Gate1Result, Stats, result_json, trade_json
from perps_agent.backtest.runner import BacktestResult

BACKTESTS_DIR = "backtests"


def _r(value: Decimal | None) -> str:
    return "–" if value is None else f"{value:+.2f}R"


def _usd(value: Decimal) -> str:
    return f"{value:+.2f}"


def _pct(value: Decimal | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def _stats_row(label: str, s: Stats) -> str:
    return (
        f"| {label} | {s.trades} | {_pct(s.win_rate)} | {_r(s.expectancy_r)} "
        f"| {_usd(s.net_pnl)} | {_usd(s.fees)} | {_usd(s.funding)} |"
    )


_STATS_HEAD = (
    "| | Trades | Win rate | Expectancy | Net PnL | Fees | Funding |\n| - | - | - | - | - | - | - |"
)


def render(result: BacktestResult, gates: Sequence[Gate1Result], symbols: Sequence[str]) -> str:
    lines = [
        f"# Playbook backtest {result.start.isoformat()} to {result.end.isoformat()}",
        "",
        f"Symbols: {', '.join(symbols)}. Sessions replayed: {len(result.sessions)}. "
        "Every setup taken as it stands (L0), one position per playbook and symbol. "
        "PnL in pUSD at the configured per-trade risk budget; R is net of fees and funding.",
        "",
        "## Gate 1",
        "",
        "| Version | Verdict | Target sessions | Trades | Expectancy | Net PnL "
        "| Largest trade | Other sessions |",
        "| - | - | - | - | - | - | - | - |",
    ]
    for g in gates:
        lines.append(
            f"| `{g.tag}` | {'pass' if g.passed else '**fail**'} | {g.target_sessions} "
            f"| {g.target.trades} | {_r(g.target.expectancy_r)} | {_usd(g.target.net_pnl)} "
            f"| {_pct(g.max_trade_share)} | {g.other.trades} ({_r(g.other.expectancy_r)}) |"
        )
    lines += [
        "",
        "Target sessions: sessions whose regime at the window end matches the playbook's "
        "declared regimes; only their trades count. The trigger review of each sample "
        "below is still to be done by hand.",
    ]
    for g in gates:
        lines += ["", f"## `{g.tag}`", ""]
        lines += [
            f"* {'pass' if c.passed else 'FAIL'} **{c.name}**: {c.detail}" for c in g.criteria
        ]
        lines.append(
            f"* Setups taken: {g.tally.signals}, of which {g.tally.unfilled} never filled."
        )
        if g.exits:
            lines.append("* Exits: " + ", ".join(f"{k} {v}" for k, v in g.exits.items()) + ".")
        lines += ["", _STATS_HEAD, _stats_row("Target sessions", g.target)]
        lines.append(_stats_row("Other sessions", g.other))
        if g.holdout_start is not None:
            lines.append(_stats_row(f"design (before {g.holdout_start})", g.design))
            lines.append(_stats_row(f"hold-out (from {g.holdout_start})", g.holdout))
        for key, s in g.by_regime.items():
            lines.append(_stats_row(f"regime {key}", s))
        for key, s in g.by_symbol.items():
            lines.append(_stats_row(key, s))
        if g.by_reach:
            lines += ["", "Design period by target distance, in expected moves over the hold:", ""]
            lines += [_STATS_HEAD] + [_stats_row(f"target {k}", s) for k, s in g.by_reach.items()]
        if g.sample:
            lines += [
                "",
                "Sample for review:",
                "",
                "| Signal (UTC) | Symbol | Side | Entry | Stop | Target | Exit | Net R | Why |",
                "| - | - | - | - | - | - | - | - | - |",
            ]
            for t in g.sample:
                target = "–" if t.target_price is None else f"{t.target_price:.4g}"
                lines.append(
                    f"| {t.signal_at:%Y-%m-%d %H:%M} | {t.symbol} | {t.direction} "
                    f"| {t.entry_price:.5g} | {t.stop_price:.5g} | {target} "
                    f"| {t.exit_reason} {t.exit_price:.5g} | {t.net_r:+.2f} "
                    f"| {'; '.join(t.reasons)} |"
                )
    return "\n".join(lines) + "\n"


def write_report(
    directory: Path,
    result: BacktestResult,
    gates: Sequence[Gate1Result],
    symbols: Sequence[str],
) -> Path:
    """``report.md``, ``report.json`` and every trade in ``trades.jsonl`` under ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.md").write_text(render(result, gates, symbols), encoding="utf-8")
    payload = {
        "window": {"start": result.start.isoformat(), "end": result.end.isoformat()},
        "symbols": list(symbols),
        "sessions": len(result.sessions),
        "gate1": [result_json(g) for g in gates],
    }
    (directory / "report.json").write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    with (directory / "trades.jsonl").open("w", encoding="utf-8") as out:
        for trade in sorted(result.trades, key=lambda t: (t.signal_at, t.playbook, t.symbol)):
            out.write(json.dumps(trade_json(trade)) + "\n")
    return directory / "report.md"
