"""Bar, history and feature builders for strategy unit tests.

The day is Tuesday 2026-10-20: the regular session opens at 13:30 UTC (09:30
New York) and the trading window ends at 16:30 UTC (12:30).
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

from perps_agent.calendar import TradingCalendar
from perps_agent.config.models import FeaturesConfig, SessionConfig
from perps_agent.strategy import Bar, FeatureSnapshot, SessionSummary, build_features
from perps_agent.strategy.playbooks import PlaybookContext
from tests.unit.risk_factories import FEES, RISK
from tests.unit.venue_factories import T0

D = Decimal
OPEN = T0
CALENDAR = TradingCalendar(SessionConfig())
WINDOW = CALENDAR.trading_window(date(2026, 10, 20))
assert WINDOW is not None
FEATURES = FeaturesConfig()
CONTEXT = PlaybookContext(window=WINDOW, risk=RISK, fees=FEES)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


def bar(
    start: datetime,
    open: str | Decimal,
    high: str | Decimal,
    low: str | Decimal,
    close: str | Decimal,
    volume: str | Decimal = "10",
) -> Bar:
    o, h, lo, c, v = (D(x) for x in (open, high, low, close, volume))
    return Bar(
        start=start, open=o, high=h, low=lo, close=c, volume=v, notional=(h + lo + c) / 3 * v
    )


def path(
    start: datetime,
    closes: Sequence[str | Decimal | float],
    *,
    first_open: str | Decimal | None = None,
    wick: str = "0.05",
    volume: str | Decimal = "10",
    volumes: Sequence[str | Decimal] | None = None,
) -> list[Bar]:
    """One bar per close, a minute apart; each opens at the previous close."""
    bars = []
    previous = D(first_open) if first_open is not None else D(str(closes[0]))
    for i, close in enumerate(closes):
        c = D(str(close))
        high = max(previous, c) + D(wick)
        low = min(previous, c) - D(wick)
        v = volumes[i] if volumes is not None else volume
        bars.append(bar(start + minutes(i), previous, high, low, c, v))
        previous = c
    return bars


def flat(start: datetime, count: int, price: str = "200", **kwargs: object) -> list[Bar]:
    return path(start, [price] * count, **kwargs)  # type: ignore[arg-type]


def summary(
    day: date,
    *,
    open: str = "200",
    high: str = "203",
    low: str = "197",
    close: str = "201",
    poc: str | None = None,
    volume: str = "3900",
) -> SessionSummary:
    return SessionSummary(
        date=day,
        open=D(open),
        high=D(high),
        low=D(low),
        close=D(close),
        vwap=D(close),
        poc=D(poc or close),
        volume=D(volume),
        minutes=390,
    )


def history(count: int = 10, **kwargs: str) -> tuple[SessionSummary, ...]:
    """``count`` prior sessions, oldest first, all alike (3% range, so 300 bp)."""
    days = []
    day = date(2026, 10, 20)
    while len(days) < count:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    return tuple(summary(d, **kwargs) for d in reversed(days))


def features(
    bars: Sequence[Bar],
    *,
    price: str | Decimal | None = None,
    at: datetime | None = None,
    prior: Sequence[SessionSummary] | None = None,
    config: FeaturesConfig = FEATURES,
) -> FeatureSnapshot:
    """Features at the end of the last bar (by default), priced at its close."""
    now = at or bars[-1].end
    return build_features(
        "NVDA-USD",
        bars=bars,
        price=D(price) if price is not None else bars[-1].close,
        now=now,
        window=WINDOW,
        history=history() if prior is None else prior,
        config=config,
    )
