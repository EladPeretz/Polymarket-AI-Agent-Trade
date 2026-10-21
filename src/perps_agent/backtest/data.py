"""Backtest history: each symbol's day as trade candles, mark bars and funding.

Live features are computed from bars that are trade candles where a minute
traded and mark-price bars (zero volume) where it did not (``MinuteBars``).
Perps trade thinly, so most minutes are marks. The backtest rebuilds the same
bars from the venue's history:

* **Trade candles** (1-minute klines) from an hour before the open to the
  close. Prior-session summaries use only these, as ``load_history`` does live.
* **Mark bars** from 1-second mark history, an hour before the open to just
  after the window end: open, high, low and close of each minute's marks.
  Features use them for untraded minutes, and stops and targets trigger on
  them, as they trigger on mark live.
* **Funding** settlements over the same span.

Days are cached as JSON under ``backtest.data_dir`` (git-ignored), and only
once they are over, so a rerun fetches only what is new. Uses the venue
interfaces only (ADR-0001).
"""

import asyncio
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from perps_agent.calendar import TradingCalendar, TradingWindow
from perps_agent.clock import from_ms, to_ms, to_utc
from perps_agent.strategy.bars import Bar, bar_from_candle, minute_of
from perps_agent.strategy.snapshot import LOOKBACK
from perps_agent.venue import FundingRate, MarketDataSource, MarkPoint

log = logging.getLogger(__name__)

CACHE_VERSION = 1
# Marks continue this long after the window end, so the close-out has a price.
AFTER_WINDOW = timedelta(minutes=5)
# Bars before the open, for the 15- and 60-minute ranges at 09:30.
BEFORE_OPEN = LOOKBACK + timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class DayData:
    symbol: str
    day: date
    # Traded minutes only, oldest first.
    candles: tuple[Bar, ...]
    # Mark-price minute bars (zero volume), oldest first; empty when not loaded.
    marks: tuple[Bar, ...]
    funding: tuple[FundingRate, ...]
    # False when the day was loaded for history only, without the marks.
    marks_loaded: bool = True

    def feature_bars(self) -> list[Bar]:
        """The bars features see live: a minute's trade candle if it traded, else its marks."""
        merged = {b.start: b for b in self.marks}
        merged.update((b.start, b) for b in self.candles)
        return [merged[start] for start in sorted(merged)]


def mark_bars(points: Iterable[MarkPoint]) -> tuple[Bar, ...]:
    """Minute bars from mark points: each minute's first, highest, lowest and last mark."""
    bars: dict[datetime, Bar] = {}
    for point in sorted(points, key=lambda p: p.timestamp):
        start = minute_of(point.timestamp)
        price = point.mark_price
        bar = bars.get(start)
        if bar is None:
            bars[start] = Bar(start, price, price, price, price, Decimal(0), Decimal(0))
        else:
            bars[start] = Bar(
                start, bar.open, max(bar.high, price), min(bar.low, price), price,
                Decimal(0), Decimal(0),
            )  # fmt: skip
    return tuple(bars[s] for s in sorted(bars))


def spans(window: TradingWindow) -> tuple[datetime, datetime, datetime]:
    """Start of the day's data, end of the candles (close) and end of marks and funding."""
    return window.market.open - BEFORE_OPEN, window.market.close, window.end + AFTER_WINDOW


class HistoryCache:
    """Loads :class:`DayData` from the cache, fetching what is missing from the venue."""

    def __init__(
        self,
        directory: Path,
        source: MarketDataSource,
        instruments: dict[str, int],
        calendar: TradingCalendar,
        *,
        max_concurrent: int = 4,
    ) -> None:
        self._dir = directory
        self._source = source
        self._instruments = instruments
        self._calendar = calendar
        self._limit = asyncio.Semaphore(max_concurrent)

    async def load(
        self, symbol: str, days: Sequence[date], *, marks: bool, now: datetime
    ) -> dict[date, DayData]:
        """Every trading day of ``days`` that has data. ``marks`` False skips the 1-second marks."""
        jobs = [self.day(symbol, d, marks=marks, now=now) for d in days]
        loaded = await asyncio.gather(*jobs)
        return {d.day: d for d in loaded if d is not None}

    async def day(self, symbol: str, day: date, *, marks: bool, now: datetime) -> DayData | None:
        window = self._calendar.trading_window(day)
        if window is None:
            return None
        path = self._dir / symbol / f"{day.isoformat()}.json"
        cached = read_day(path)
        if cached is not None and (cached.marks_loaded or not marks):
            return cached
        start, candles_end, marks_end = spans(window)
        async with self._limit:
            data = await self._fetch(symbol, day, window, marks=marks)
        if to_utc(now) >= max(candles_end, marks_end):
            write_day(path, data)
        else:
            log.info("day not over, not cached", extra={"symbol": symbol, "day": str(day)})
        return data

    async def _fetch(
        self, symbol: str, day: date, window: TradingWindow, *, marks: bool
    ) -> DayData:
        instrument = self._instruments[symbol]
        start, candles_end, marks_end = spans(window)
        candles = await self._source.fetch_candles(instrument, "1m", start, candles_end)
        points: tuple[MarkPoint, ...] = ()
        if marks:
            points = await self._source.fetch_mark_history(instrument, "1s", start, marks_end)
        funding = await self._source.fetch_funding_history(instrument, start, marks_end)
        log.info(
            "fetched day",
            extra={
                "symbol": symbol,
                "day": str(day),
                "candles": len(candles),
                "marks": len(points),
            },
        )
        return DayData(
            symbol=symbol,
            day=day,
            candles=tuple(bar_from_candle(c) for c in candles if c.volume > 0),
            marks=mark_bars(points),
            funding=funding,
            marks_loaded=marks,
        )


def _bar_row(bar: Bar) -> list[Any]:
    return [
        to_ms(bar.start), str(bar.open), str(bar.high), str(bar.low), str(bar.close),
        str(bar.volume), str(bar.notional),
    ]  # fmt: skip


def _bar(row: Sequence[Any]) -> Bar:
    ts, *values = row
    o, h, lo, c, v, n = (Decimal(x) for x in values)
    return Bar(from_ms(int(ts)), o, h, lo, c, v, n)


def write_day(path: Path, data: DayData) -> None:
    payload = {
        "version": CACHE_VERSION,
        "symbol": data.symbol,
        "day": data.day.isoformat(),
        "candles": [_bar_row(b) for b in data.candles],
        "marks": [_bar_row(b) for b in data.marks] if data.marks_loaded else None,
        "funding": [[to_ms(f.timestamp), f.instrument_id, str(f.rate)] for f in data.funding],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def read_day(path: Path) -> DayData | None:
    """A cached day, or ``None`` if it is missing, unreadable or from an older cache version."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    return DayData(
        symbol=payload["symbol"],
        day=date.fromisoformat(payload["day"]),
        candles=tuple(_bar(r) for r in payload["candles"]),
        marks=tuple(_bar(r) for r in payload["marks"] or ()),
        funding=tuple(
            FundingRate(instrument_id=iid, rate=Decimal(rate), timestamp=from_ms(int(ts)))
            for ts, iid, rate in payload["funding"]
        ),
        marks_loaded=payload["marks"] is not None,
    )
