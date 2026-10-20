"""Session calendar: NYSE trading days and the bot's trading window (overview §3.3).

Computed locally because the venue does not expose market sessions.
"""

from perps_agent.calendar.nyse import (
    SPECIAL_CLOSURES,
    SUPPORTED_YEARS,
    CalendarError,
    MarketSession,
    NyseCalendar,
    TradingDay,
    early_closes,
    holidays,
)
from perps_agent.calendar.window import TradingCalendar, TradingWindow

__all__ = [
    "SPECIAL_CLOSURES",
    "SUPPORTED_YEARS",
    "CalendarError",
    "MarketSession",
    "NyseCalendar",
    "TradingCalendar",
    "TradingDay",
    "TradingWindow",
    "early_closes",
    "holidays",
]
