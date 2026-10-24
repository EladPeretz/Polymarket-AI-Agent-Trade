"""``perps-agent backtest``: the Gate 1 screen over the rolling window (ADR-0008).

Loads (and caches) the window's history for every enabled symbol, replays the
playbooks, writes a Gate 1 record per playbook version to
``<results_dir>/gate1/`` and the full report to
``<results_dir>/backtests/<run date>/``. Read-only market data, no credentials.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from perps_agent.backtest.data import AFTER_WINDOW, DayData, HistoryCache
from perps_agent.backtest.gate import Gate1Result, evaluate, write_records
from perps_agent.backtest.report import BACKTESTS_DIR, write_report
from perps_agent.backtest.runner import Backtester, BacktestResult
from perps_agent.calendar import TradingCalendar
from perps_agent.clock import to_utc
from perps_agent.config.models import AppConfig
from perps_agent.strategy.history import prior_sessions
from perps_agent.strategy.playbooks import Playbook
from perps_agent.venue import InstrumentRegistry, MarketDataSource, create_market_data

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BacktestRun:
    result: BacktestResult
    gates: tuple[Gate1Result, ...]
    report: Path
    records: tuple[Path, ...]


def window_days(config: AppConfig, calendar: TradingCalendar, now: datetime) -> list[date]:
    """Trading days in the last ``backtest.window_days``; today only once its window is over."""
    now = to_utc(now)
    today = now.astimezone(config.session.zone).date()
    days = []
    for offset in range(config.backtest.window_days, -1, -1):
        day = today - timedelta(days=offset)
        window = calendar.trading_window(day)
        if window is not None and window.end + AFTER_WINDOW <= now:
            days.append(day)
    return days


async def load_data(
    config: AppConfig,
    calendar: TradingCalendar,
    source: MarketDataSource,
    symbols: Sequence[str],
    days: Sequence[date],
    now: datetime,
) -> dict[str, dict[date, DayData]]:
    """Each symbol's window days (with marks) and the history days before them (candles only)."""
    registry = InstrumentRegistry(source)
    await registry.refresh()
    instruments = {s: registry.by_symbol(s).instrument_id for s in symbols}
    cache = HistoryCache(
        Path(config.backtest.data_dir),
        source,
        instruments,
        calendar,
        max_concurrent=config.backtest.max_concurrent_requests,
    )
    history = prior_sessions(calendar.exchange, days[0], config.features.history_sessions)

    async def one(symbol: str) -> dict[date, DayData]:
        loaded = await cache.load(symbol, history, marks=False, now=now)
        loaded.update(await cache.load(symbol, days, marks=True, now=now))
        return loaded

    loaded = await asyncio.gather(*(one(s) for s in symbols))
    return dict(zip(symbols, loaded, strict=True))


async def run_backtest(
    config: AppConfig,
    playbooks: Sequence[Playbook[Any]],
    *,
    now: datetime,
    symbols: Sequence[str] | None = None,
    source: MarketDataSource | None = None,
) -> BacktestRun:
    calendar = TradingCalendar(config.session)
    symbols = tuple(symbols or config.enabled_symbols)
    days = window_days(config, calendar, now)
    if not days:
        raise ValueError("the backtest window has no completed trading days")
    owned = source is None
    source = source or create_market_data(config.venue)
    try:
        data = await load_data(config, calendar, source, symbols, days, now)
    finally:
        if owned:
            await source.aclose()
    result = Backtester(config, calendar, playbooks).run(data, days)
    gates = tuple(evaluate(p, result, config.backtest, now) for p in playbooks)
    results_dir = Path(config.agent.results_dir)
    run_day = to_utc(now).astimezone(config.session.zone).date()
    report = write_report(results_dir / BACKTESTS_DIR / run_day.isoformat(), result, gates, symbols)
    records = tuple(write_records(results_dir, gates))
    log.info(
        "backtest done",
        extra={
            "trades": len(result.trades),
            "passed": [g.tag for g in gates if g.passed],
            "report": str(report),
        },
    )
    return BacktestRun(result=result, gates=gates, report=report, records=records)
