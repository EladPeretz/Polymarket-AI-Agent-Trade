"""Daily report (overview §3.12): one session's results, from its files in ``results/<date>/``."""

from perps_agent.report.daily import (
    REPORT_JSON,
    REPORT_MD,
    Attribution,
    DailyReport,
    LatencyStats,
    TradeLine,
    build_report,
)
from perps_agent.report.equity import (
    EQUITY_FILE,
    EquitySample,
    JsonlEquitySink,
    load_equity,
    max_drawdown,
)
from perps_agent.report.render import render_markdown, write_report

__all__ = [
    "EQUITY_FILE",
    "REPORT_JSON",
    "REPORT_MD",
    "Attribution",
    "DailyReport",
    "EquitySample",
    "JsonlEquitySink",
    "LatencyStats",
    "TradeLine",
    "build_report",
    "load_equity",
    "max_drawdown",
    "render_markdown",
    "write_report",
]
