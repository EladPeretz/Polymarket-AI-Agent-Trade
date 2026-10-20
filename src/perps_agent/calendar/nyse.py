"""NYSE trading days, holidays, early closes and session categories, computed locally.

The venue does not expose the market session (venue research §6), so it is derived
from the NYSE rules here. The rules (NYSE Rule 7.2, holiday observance and the
13:00 early closes) are applied for the years in ``SUPPORTED_YEARS``; dates outside
that range raise :class:`CalendarError` so a stale calendar fails loudly instead of
silently guessing. Unscheduled closures (e.g. national days of mourning) are listed
in ``SPECIAL_CLOSURES`` or added through ``session.extra_closures`` in the config.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import cache
from zoneinfo import ZoneInfo

from perps_agent.clock import to_utc

NEW_YORK = ZoneInfo("America/New_York")

# Juneteenth became an NYSE holiday in 2022. Re-check the rules against NYSE's
# published holiday list before extending the upper bound.
SUPPORTED_YEARS = range(2022, 2031)

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
# Extended hours used to label the session. They do not gate trading: the
# trading window (``TradingCalendar``) does.
PREMARKET_OPEN = time(4, 0)
POSTMARKET_CLOSE = time(20, 0)
EARLY_POSTMARKET_CLOSE = time(17, 0)

# Unscheduled full-day closures, by date.
SPECIAL_CLOSURES: dict[date, str] = {
    date(2025, 1, 9): "National Day of Mourning for President Carter",
}


class CalendarError(ValueError):
    """The date is outside the range the calendar rules are known to hold for."""


class MarketSession(StrEnum):
    """The underlying's session, mirroring the venue's session categories.

    ``CLOSED`` covers an off-hours gap that includes an exchange holiday. The
    venue's ``Halted`` and ``Unknown`` categories cannot be derived from a
    calendar and are not produced.
    """

    PREMARKET = "premarket"
    REGULAR = "regular"
    POSTMARKET = "postmarket"
    OVERNIGHT = "overnight"
    WEEKEND = "weekend"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class TradingDay:
    """One NYSE trading day. Datetimes are timezone-aware UTC."""

    date: date
    open: datetime
    close: datetime
    early_close: bool

    @property
    def premarket_open(self) -> datetime:
        return local_to_utc(self.date, PREMARKET_OPEN)

    @property
    def postmarket_close(self) -> datetime:
        close = EARLY_POSTMARKET_CLOSE if self.early_close else POSTMARKET_CLOSE
        return local_to_utc(self.date, close)


def local_to_utc(day: date, at: time, zone: ZoneInfo = NEW_YORK) -> datetime:
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)


def _check_year(year: int) -> None:
    if year not in SUPPORTED_YEARS:
        raise CalendarError(
            f"NYSE calendar rules are only verified for {SUPPORTED_YEARS.start}-"
            f"{SUPPORTED_YEARS.stop - 1}, not {year}"
        )


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Monday=0) of the month; ``n=-1`` is the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    following = date(year + month // 12, month % 12 + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """Saturday holidays move to Friday, Sunday holidays to Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@cache
def holidays(year: int) -> dict[date, str]:
    """Scheduled full-day NYSE holidays falling in ``year``, by observed date."""
    _check_year(year)
    days = {
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        _easter(year) - timedelta(days=2): "Good Friday",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        _observed(date(year, 6, 19)): "Juneteenth National Independence Day",
        _observed(date(year, 7, 4)): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        _observed(date(year, 12, 25)): "Christmas Day",
    }
    # New Year's Day on a Saturday is not observed on the preceding Friday
    # (NYSE Rule 7.2), so it is only moved forward from a Sunday.
    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:
        new_year += timedelta(days=1)
    if new_year.weekday() < 5:
        days[new_year] = "New Year's Day"
    return days


@cache
def early_closes(year: int) -> frozenset[date]:
    """Scheduled 13:00 closes: before Independence Day, after Thanksgiving, Christmas Eve."""
    _check_year(year)
    closed = holidays(year)
    candidates = (
        date(year, 7, 3),
        _nth_weekday(year, 11, 3, 4) + timedelta(days=1),
        date(year, 12, 24),
    )
    # A Friday 3 July or 24 December is the observed holiday itself, so it drops out here.
    return frozenset(day for day in candidates if day.weekday() < 5 and day not in closed)


class NyseCalendar:
    """NYSE trading days and session categories (America/New_York)."""

    def __init__(self, extra_closures: Iterable[date] = ()) -> None:
        self._extra_closures = frozenset(extra_closures)

    def closure_reason(self, day: date) -> str | None:
        """Why the exchange is closed on ``day``, or ``None`` on a trading day."""
        _check_year(day.year)
        if day.weekday() >= 5:
            return "weekend"
        if day in self._extra_closures:
            return "unscheduled closure (config)"
        if day in SPECIAL_CLOSURES:
            return SPECIAL_CLOSURES[day]
        return holidays(day.year).get(day)

    def is_trading_day(self, day: date) -> bool:
        return self.closure_reason(day) is None

    def trading_day(self, day: date) -> TradingDay | None:
        """Regular-session hours on ``day``, or ``None`` when the exchange is closed."""
        if not self.is_trading_day(day):
            return None
        early = day in early_closes(day.year)
        return TradingDay(
            date=day,
            open=local_to_utc(day, REGULAR_OPEN),
            close=local_to_utc(day, EARLY_CLOSE if early else REGULAR_CLOSE),
            early_close=early,
        )

    def next_trading_day(self, after: date) -> date:
        day = after + timedelta(days=1)
        while not self.is_trading_day(day):
            day += timedelta(days=1)
        return day

    def previous_trading_day(self, before: date) -> date:
        day = before - timedelta(days=1)
        while not self.is_trading_day(day):
            day -= timedelta(days=1)
        return day

    def session_at(self, now: datetime) -> MarketSession:
        """The session category at ``now`` (an aware datetime)."""
        now = to_utc(now)
        today = now.astimezone(NEW_YORK).date()
        hours = self.trading_day(today)
        if hours is not None:
            if hours.premarket_open <= now < hours.open:
                return MarketSession.PREMARKET
            if hours.open <= now < hours.close:
                return MarketSession.REGULAR
            if hours.close <= now < hours.postmarket_close:
                return MarketSession.POSTMARKET

        # Off hours: find the trading days on either side of the gap.
        if hours is not None and now >= hours.postmarket_close:
            before, after = today, self.next_trading_day(today)
        elif hours is not None:
            before, after = self.previous_trading_day(today), today
        else:
            before, after = self.previous_trading_day(today), self.next_trading_day(today)
        if (after - before).days == 1:
            return MarketSession.OVERNIGHT
        gap = (before + timedelta(days=n) for n in range(1, (after - before).days))
        if all(day.weekday() >= 5 for day in gap):
            return MarketSession.WEEKEND
        return MarketSession.CLOSED
