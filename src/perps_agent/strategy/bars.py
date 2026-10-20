"""One-minute bars, the input every feature is computed from (overview §3.4).

Bars come from two places that must agree: REST 1-minute candles for history
(seeded at startup and for backtests) and the live trades stream. A bar built
from trades has the exact notional; one from a candle estimates it from the
typical price ``(high + low + close) / 3``. Minutes without trades have no bar.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from perps_agent.clock import to_utc
from perps_agent.venue import Candle, Trade

MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Bar:
    """Trades in ``[start, start + 1 minute)``. ``volume`` is in base units."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    # Σ price × quantity, for VWAP.
    notional: Decimal

    @property
    def end(self) -> datetime:
        return self.start + MINUTE

    @property
    def typical(self) -> Decimal:
        return (self.high + self.low + self.close) / 3

    @property
    def average_price(self) -> Decimal:
        return self.notional / self.volume if self.volume > 0 else self.close


def minute_of(at: datetime) -> datetime:
    """The start of the minute containing ``at`` (UTC)."""
    at = to_utc(at)
    return at.replace(second=0, microsecond=0)


def bar_from_candle(candle: Candle) -> Bar:
    if candle.interval != "1m":
        raise ValueError(f"features use 1m candles, got {candle.interval}")
    typical = (candle.high + candle.low + candle.close) / 3
    return Bar(
        start=candle.open_time,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        notional=typical * candle.volume,
    )


class MinuteBars:
    """A symbol's 1-minute bars, oldest first, seeded from candles and extended by trades.

    Trades are deduplicated by id. A candle already contains its minute's
    trades, so seed candles only for minutes before the first streamed trade;
    a trade for a seeded minute is merged into that bar.
    """

    def __init__(self, *, keep: timedelta = timedelta(days=1)) -> None:
        self._bars: dict[datetime, Bar] = {}
        self._seen: set[int] = set()
        self._keep = keep

    def seed(self, candles: Iterable[Candle]) -> None:
        for candle in candles:
            bar = bar_from_candle(candle)
            self._bars[bar.start] = bar

    def add_trades(self, trades: Iterable[Trade]) -> None:
        for trade in trades:
            if trade.trade_id in self._seen or trade.settlement:
                continue
            self._seen.add(trade.trade_id)
            start = minute_of(trade.timestamp)
            notional = trade.price * trade.quantity
            bar = self._bars.get(start)
            if bar is None:
                self._bars[start] = Bar(
                    start=start,
                    open=trade.price,
                    high=trade.price,
                    low=trade.price,
                    close=trade.price,
                    volume=trade.quantity,
                    notional=notional,
                )
                continue
            # Trades can arrive slightly out of order; open/close follow arrival
            # within a minute, which is close enough for minute features.
            self._bars[start] = Bar(
                start=start,
                open=bar.open,
                high=max(bar.high, trade.price),
                low=min(bar.low, trade.price),
                close=trade.price,
                volume=bar.volume + trade.quantity,
                notional=bar.notional + notional,
            )

    def prune(self, now: datetime) -> None:
        cutoff = to_utc(now) - self._keep
        for start in [s for s in self._bars if s < cutoff]:
            del self._bars[start]
        if len(self._seen) > 200_000:
            self._seen.clear()

    def bars(self, *, start: datetime | None = None, end: datetime | None = None) -> list[Bar]:
        """Bars starting in ``[start, end)``, oldest first."""
        return [
            bar
            for key, bar in sorted(self._bars.items())
            if (start is None or key >= start) and (end is None or key < end)
        ]


def between(bars: Sequence[Bar], start: datetime, end: datetime) -> list[Bar]:
    """The bars of ``bars`` (oldest first) starting in ``[start, end)``."""
    return [b for b in bars if start <= b.start < end]
