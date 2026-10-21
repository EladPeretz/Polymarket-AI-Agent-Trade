"""Rehearsal sessions: the full paper session on a short window that starts now.

A rehearsal runs the same engine, feed, simulator, guardian, bridge and report
as ``perps-agent run``, against the live market data (perps trade 24/7), but on
a compressed window starting at the next minute instead of 09:30–12:30 New
York. It exists to exercise the whole session outside market hours (WP12 dry
run) and after changes to the engine.

Results go to ``data/rehearsals/<UTC timestamp>/<date>/``, never to the results
directory, so a rehearsal can never be published to the results branch, count
in the briefing's recent activity, or block the day's real session.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

from perps_agent.calendar import TradingCalendar, TradingWindow
from perps_agent.calendar.nyse import NEW_YORK, TradingDay
from perps_agent.clock import to_utc
from perps_agent.config import AppConfig, SessionConfig

REHEARSAL_DIR = Path("data/rehearsals")
MIN_MINUTES = 10
MAX_MINUTES = 180
# Entries stop this long before the window end, and the guardian's hard
# deadline is this long after it (09:30–12:15–12:30–12:35 scaled down).
_CUTOFF_BEFORE_END = timedelta(minutes=3)
_DEADLINE_AFTER_END = timedelta(minutes=2)


def rehearsal_window(now: datetime, minutes: int) -> TradingWindow:
    """A window of ``minutes`` starting at the next whole minute after ``now``."""
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise ValueError(f"a rehearsal lasts {MIN_MINUTES} to {MAX_MINUTES} minutes")
    now = to_utc(now)
    start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = start + timedelta(minutes=minutes)
    day = start.astimezone(NEW_YORK).date()
    return TradingWindow(
        date=day,
        start=start,
        entry_cutoff=end - _CUTOFF_BEFORE_END,
        end=end,
        hard_flat_deadline=end + _DEADLINE_AFTER_END,
        # The "regular session" the features measure starts with the window.
        market=TradingDay(date=day, open=start, close=end, early_close=False),
    )


class RehearsalCalendar(TradingCalendar):
    """The real NYSE calendar, except that the rehearsal window is the only window.

    History, key levels and the ticker profile still come from real prior
    sessions (``exchange``); everything that asks "is the window open now"
    sees the rehearsal window, even across New York midnight.
    """

    def __init__(self, session: SessionConfig, window: TradingWindow) -> None:
        super().__init__(session)
        self._window = window

    @property
    def window(self) -> TradingWindow:
        return self._window

    def trading_window(self, day: date) -> TradingWindow | None:
        return self._window if day == self._window.date else None

    def window_at(self, now: datetime) -> TradingWindow | None:
        return self._window


def rehearsal_config(config: AppConfig, now: datetime) -> AppConfig:
    """``config`` with results under a fresh rehearsal directory."""
    results = REHEARSAL_DIR / to_utc(now).strftime("%Y%m%dT%H%M%SZ")
    agent = config.agent.model_copy(update={"results_dir": str(results)})
    return config.model_copy(update={"agent": agent})
