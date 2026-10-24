"""The bot's daily trading window on NYSE trading days (overview §3.3, §4).

Positions exist only inside the window: entries are allowed from ``start`` until
``entry_cutoff``, only exits after that, and from ``end`` the bot must be flat
(the guardian closes everything by ``hard_flat_deadline``). Outside the window,
including every non-trading day, :meth:`TradingCalendar.must_be_flat` is true.
"""

from dataclasses import dataclass
from datetime import date, datetime, time

from perps_agent.calendar.nyse import (
    NEW_YORK,
    MarketSession,
    NyseCalendar,
    TradingDay,
    local_to_utc,
)
from perps_agent.clock import to_utc
from perps_agent.config import SessionConfig


@dataclass(frozen=True, slots=True)
class TradingWindow:
    """One day's window. Datetimes are timezone-aware UTC."""

    date: date
    start: datetime
    entry_cutoff: datetime
    end: datetime
    hard_flat_deadline: datetime
    market: TradingDay

    def contains(self, now: datetime) -> bool:
        return self.start <= to_utc(now) < self.end


class TradingCalendar:
    """The configured trading window applied to the NYSE calendar."""

    def __init__(self, session: SessionConfig, exchange: NyseCalendar | None = None) -> None:
        self._session = session
        self._exchange = exchange or NyseCalendar(session.extra_closures)

    @property
    def exchange(self) -> NyseCalendar:
        return self._exchange

    def trading_window(self, day: date) -> TradingWindow | None:
        """The window on ``day`` (an exchange date), or ``None`` on non-trading days.

        On early-close days every window time is capped at the regular-session
        close, so the bot is flat before the underlying stops trading. A day whose
        window would start at or after the close has no window.
        """
        market = self._exchange.trading_day(day)
        if market is None:
            return None
        start = self._local(day, self._session.window_start)
        if start >= market.close:
            return None
        return TradingWindow(
            date=day,
            start=start,
            entry_cutoff=min(self._local(day, self._session.entry_cutoff), market.close),
            end=min(self._local(day, self._session.window_end), market.close),
            hard_flat_deadline=min(
                self._local(day, self._session.hard_flat_deadline), market.close
            ),
            market=market,
        )

    def window_at(self, now: datetime) -> TradingWindow | None:
        """The window of the exchange date at ``now``, if that day has one."""
        return self.trading_window(to_utc(now).astimezone(NEW_YORK).date())

    def is_entry_allowed(self, now: datetime) -> bool:
        """True from the window start until the entry cutoff on a trading day."""
        window = self.window_at(now)
        return window is not None and window.start <= to_utc(now) < window.entry_cutoff

    def must_be_flat(self, now: datetime) -> bool:
        """True whenever ``now`` is outside the trading window, including non-trading days."""
        window = self.window_at(now)
        return window is None or not window.contains(now)

    def session_at(self, now: datetime) -> MarketSession:
        return self._exchange.session_at(now)

    def _local(self, day: date, at: time) -> datetime:
        return local_to_utc(day, at, self._session.zone)
