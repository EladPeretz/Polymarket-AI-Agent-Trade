"""Playbook backtests and the Gate 1 screen (overview §5, go-no-go.md, ADR-0008).

Playbooks only, no LLM: history is replayed through the same features and
playbooks paper trading runs, every setup is taken as it stands, and each
playbook version gets a Gate 1 verdict.
"""

from perps_agent.backtest.data import DayData, HistoryCache, mark_bars, read_day, write_day
from perps_agent.backtest.gate import (
    GATE1_DIR,
    Criterion,
    Gate1Result,
    Stats,
    evaluate,
    read_records,
    screened_playbooks,
    write_records,
)
from perps_agent.backtest.report import BACKTESTS_DIR, render, write_report
from perps_agent.backtest.runner import (
    Backtester,
    BacktestResult,
    BacktestTrade,
    ExitReason,
    PlaybookTally,
    SessionInfo,
)

__all__ = [
    "BACKTESTS_DIR",
    "GATE1_DIR",
    "BacktestResult",
    "BacktestTrade",
    "Backtester",
    "Criterion",
    "DayData",
    "ExitReason",
    "Gate1Result",
    "HistoryCache",
    "PlaybookTally",
    "SessionInfo",
    "Stats",
    "evaluate",
    "mark_bars",
    "read_day",
    "read_records",
    "render",
    "screened_playbooks",
    "write_day",
    "write_records",
    "write_report",
]
