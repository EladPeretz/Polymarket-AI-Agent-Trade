"""Loading bar history and prior-session summaries from the venue's 1-minute candles.

Uses the venue interfaces only (ADR-0001). Perps trade around the clock, so
the bars before today's open (for the 15- and 60-minute ranges at 09:30) come
from the overnight session.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from perps_agent.calendar import NyseCalendar
from perps_agent.clock import to_utc
from perps_agent.config.models import FeaturesConfig
from perps_agent.strategy.bars import Bar, bar_from_candle
from perps_agent.strategy.features import SessionSummary, summarize_session
from perps_agent.venue import MarketDataSource


async def load_bars(
    source: MarketDataSource, instrument_id: int, start: datetime, end: datetime
) -> list[Bar]:
    candles = await source.fetch_candles(instrument_id, "1m", to_utc(start), to_utc(end))
    return [bar_from_candle(c) for c in candles]


def prior_sessions(exchange: NyseCalendar, today: date, count: int) -> list[date]:
    """The ``count`` trading days before ``today``, oldest first."""
    days: list[date] = []
    day = today
    while len(days) < count:
        day = exchange.previous_trading_day(day)
        days.append(day)
    return days[::-1]


def summarize_sessions(
    bars: Sequence[Bar], exchange: NyseCalendar, days: Sequence[date], config: FeaturesConfig
) -> tuple[SessionSummary, ...]:
    """Summaries of the regular sessions on ``days`` that have bars, oldest first."""
    summaries = []
    for day in days:
        market = exchange.trading_day(day)
        if market is None:
            continue
        summary = summarize_session(bars, market, config)
        if summary is not None:
            summaries.append(summary)
    return tuple(summaries)


async def load_history(
    source: MarketDataSource,
    instrument_id: int,
    exchange: NyseCalendar,
    today: date,
    config: FeaturesConfig,
) -> tuple[SessionSummary, ...]:
    """Prior-session summaries for ``today``'s features (``history_sessions`` of them)."""
    summaries: list[SessionSummary] = []
    for day in prior_sessions(exchange, today, config.history_sessions):
        market = exchange.trading_day(day)
        assert market is not None
        bars = await load_bars(source, instrument_id, market.open, market.close)
        summary = summarize_session(bars, market, config)
        if summary is not None:
            summaries.append(summary)
    return tuple(summaries)


async def load_recent_bars(
    source: MarketDataSource, instrument_id: int, now: datetime, lookback: timedelta
) -> list[Bar]:
    """Bars for the ``lookback`` before ``now``, to seed live features at startup."""
    now = to_utc(now)
    return await load_bars(source, instrument_id, now - lookback, now)
