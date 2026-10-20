"""Deterministic features: every snapshot the agent and the playbooks see (overview §3.4).

All features are pure functions of 1-minute bars, the current price and the
day's trading window, so backtests compute exactly what live trading does.

* **Volatility** (owner decision, WP9): the 15- and 60-minute high-low ranges
  and the expected move to the window end, ``σ₁ₘ × √minutes left`` from the
  1-minute log-return volatility. The risk engine's volatility stop floor uses
  the 15-minute range.
* **Session**: the regular session so far (from the NYSE open): open, high,
  low, VWAP, price against the open and VWAP, VWAP crosses, opening range.
* **Volume profile**: volume by price bucket over the session; POC (point of
  control) and value area.
* **Key levels**: the prior session's high, low, close and POC, and today's
  opening range and extremes; the support and resistance a sweep is judged on.
* **Ticker profile**: how the symbol has traded over recent sessions: returns,
  usual range, and whether it tends to trend or consolidate.

A feature that cannot be computed yet (no bars, no history) is ``None``;
consumers treat ``None`` as "not known", never as zero.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from perps_agent.calendar import TradingDay, TradingWindow
from perps_agent.clock import to_utc
from perps_agent.config.models import FeaturesConfig
from perps_agent.strategy.bars import MINUTE, Bar, between

BP = Decimal(10_000)
ZERO = Decimal(0)


def bp(part: Decimal, whole: Decimal) -> Decimal:
    """``part`` as basis points of ``whole``."""
    return part / whole * BP


@dataclass(frozen=True, slots=True)
class VolatilityFeatures:
    """Recent volatility. Ranges are high − low (price units) and the same in bp."""

    range_15m: Decimal | None
    range_15m_bp: Decimal | None
    range_60m: Decimal | None
    range_60m_bp: Decimal | None
    # Standard deviation of 1-minute log returns, in bp.
    minute_vol_bp: Decimal | None
    minutes_to_close: int
    # One standard deviation of the move left until the window end.
    expected_move_to_close: Decimal | None
    expected_move_to_close_bp: Decimal | None


@dataclass(frozen=True, slots=True)
class VolumeProfile:
    poc: Decimal
    value_area_low: Decimal
    value_area_high: Decimal
    bucket_size: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class SessionFeatures:
    """Today's regular session up to now."""

    started_at: datetime
    minutes_elapsed: int
    open: Decimal
    high: Decimal
    low: Decimal
    vwap: Decimal
    volume: Decimal
    price_vs_open_bp: Decimal
    price_vs_vwap_bp: Decimal
    range_bp: Decimal
    # |price − open| / (high − low): 1 is a one-way session, near 0 a rotation.
    efficiency: Decimal
    vwap_crosses: int
    # Share of bars that closed above the running VWAP.
    share_above_vwap: Decimal
    # None if no bar traded in the opening range.
    opening_range_high: Decimal | None
    opening_range_low: Decimal | None
    opening_range_complete: bool
    # Volume against prior sessions' average, pro rata for the time elapsed.
    relative_volume: Decimal | None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """A completed regular session, for history and key levels."""

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vwap: Decimal
    poc: Decimal
    volume: Decimal
    minutes: int

    @property
    def range_bp(self) -> Decimal:
        return bp(self.high - self.low, self.open)

    @property
    def return_bp(self) -> Decimal:
        return bp(self.close - self.open, self.open)

    @property
    def efficiency(self) -> Decimal:
        span = self.high - self.low
        return abs(self.close - self.open) / span if span > 0 else ZERO


class TickerCharacter(StrEnum):
    TRENDING = "trending"
    CONSOLIDATING = "consolidating"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class TickerProfile:
    """Recent performance and character, from prior sessions (newest last)."""

    sessions: int
    # Current price against the last session's close, and the close five sessions ago.
    return_1d_bp: Decimal
    return_5d_bp: Decimal | None
    average_range_bp: Decimal
    average_efficiency: Decimal
    up_sessions: int
    character: TickerCharacter


@dataclass(frozen=True, slots=True)
class KeyLevel:
    name: str
    price: Decimal


def volatility_features(
    bars: Sequence[Bar],
    price: Decimal,
    now: datetime,
    window: TradingWindow | None,
    config: FeaturesConfig,
) -> VolatilityFeatures:
    now = to_utc(now)

    def span(minutes: int) -> Decimal | None:
        recent = between(bars, now - timedelta(minutes=minutes), now)
        if not recent:
            return None
        return max(max(b.high for b in recent), price) - min(min(b.low for b in recent), price)

    range_15m, range_60m = span(15), span(60)
    vol = minute_volatility(
        between(bars, now - timedelta(minutes=config.realized_vol_minutes), now)
    )
    minutes_left = 0
    if window is not None and now < window.end:
        minutes_left = int((window.end - max(now, window.start)) / MINUTE)
    expected = None if vol is None else price * vol * Decimal(minutes_left).sqrt()
    return VolatilityFeatures(
        range_15m=range_15m,
        range_15m_bp=None if range_15m is None else bp(range_15m, price),
        range_60m=range_60m,
        range_60m_bp=None if range_60m is None else bp(range_60m, price),
        minute_vol_bp=None if vol is None else vol * BP,
        minutes_to_close=minutes_left,
        expected_move_to_close=expected,
        expected_move_to_close_bp=None if expected is None else bp(expected, price),
    )


def minute_volatility(bars: Sequence[Bar], *, minimum: int = 10) -> Decimal | None:
    """Sample standard deviation of close-to-close log returns (a fraction), or ``None``."""
    returns = [(b.close / a.close).ln() for a, b in zip(bars, bars[1:], strict=False)]
    if len(returns) < minimum:
        return None
    mean = sum(returns, ZERO) / len(returns)
    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / (len(returns) - 1)
    return variance.sqrt()


def volume_profile(
    bars: Sequence[Bar], bucket_size: Decimal, value_area_fraction: Decimal
) -> VolumeProfile | None:
    """Volume by price bucket (each bar's volume at its average price).

    The value area grows from the POC one bucket at a time, taking the heavier
    neighbour, until it holds ``value_area_fraction`` of the volume.
    """
    if bucket_size <= 0:
        raise ValueError("bucket_size must be positive")
    buckets: dict[int, Decimal] = {}
    for bar in bars:
        if bar.volume > 0:
            key = int(bar.average_price // bucket_size)
            buckets[key] = buckets.get(key, ZERO) + bar.volume
    total = sum(buckets.values(), ZERO)
    if total <= 0:
        return None
    # Ties go to the lower price, so the result does not depend on dict order.
    poc = min(buckets, key=lambda k: (-buckets[k], k))
    first, last = min(buckets), max(buckets)
    low = high = poc
    inside = buckets[poc]
    while inside < total * value_area_fraction and (low > first or high < last):
        below = buckets.get(low - 1, ZERO) if low > first else Decimal(-1)
        above = buckets.get(high + 1, ZERO) if high < last else Decimal(-1)
        if above > below:
            high += 1
            inside += above
        else:
            low -= 1
            inside += below

    def middle(key: int) -> Decimal:
        return (Decimal(key) + Decimal("0.5")) * bucket_size

    return VolumeProfile(
        poc=middle(poc),
        value_area_low=Decimal(low) * bucket_size,
        value_area_high=Decimal(high + 1) * bucket_size,
        bucket_size=bucket_size,
        volume=total,
    )


def session_features(
    bars: Sequence[Bar],
    price: Decimal,
    now: datetime,
    market: TradingDay,
    history: Sequence[SessionSummary],
    config: FeaturesConfig,
) -> SessionFeatures | None:
    """Today's session up to ``now``; ``None`` before the first bar after the open."""
    now = to_utc(now)
    session = between(bars, market.open, min(now, market.close))
    if not session:
        return None
    volume = sum((b.volume for b in session), ZERO)
    notional = sum((b.notional for b in session), ZERO)
    vwap = notional / volume if volume > 0 else session[-1].close
    high = max(max(b.high for b in session), price)
    low = min(min(b.low for b in session), price)
    first = session[0].open

    crosses, above, side = 0, 0, 0
    running_volume, running_notional = ZERO, ZERO
    for bar in session:
        running_volume += bar.volume
        running_notional += bar.notional
        running_vwap = running_notional / running_volume if running_volume > 0 else bar.close
        now_side = (bar.close > running_vwap) - (bar.close < running_vwap)
        if now_side > 0:
            above += 1
        if now_side and side and now_side != side:
            crosses += 1
        side = now_side or side

    opening_end = market.open + timedelta(minutes=config.opening_range_minutes)
    opening = [b for b in session if b.start < opening_end]
    elapsed = int((now - market.open) / MINUTE)
    session_minutes = int((market.close - market.open) / MINUTE)
    relative = None
    usual = [h.volume / h.minutes for h in history if h.minutes > 0 and h.volume > 0]
    if usual and elapsed > 0:
        relative = volume / (sum(usual, ZERO) / len(usual) * min(elapsed, session_minutes))
    span = high - low
    return SessionFeatures(
        started_at=market.open,
        minutes_elapsed=elapsed,
        open=first,
        high=high,
        low=low,
        vwap=vwap,
        volume=volume,
        price_vs_open_bp=bp(price - first, first),
        price_vs_vwap_bp=bp(price - vwap, vwap),
        range_bp=bp(span, first),
        efficiency=abs(price - first) / span if span > 0 else ZERO,
        vwap_crosses=crosses,
        share_above_vwap=Decimal(above) / len(session),
        opening_range_high=max((b.high for b in opening), default=None),
        opening_range_low=min((b.low for b in opening), default=None),
        opening_range_complete=now >= opening_end,
        relative_volume=relative,
    )


def summarize_session(
    bars: Sequence[Bar], market: TradingDay, config: FeaturesConfig
) -> SessionSummary | None:
    """A completed regular session from its bars, or ``None`` if it has none."""
    session = between(bars, market.open, market.close)
    if not session:
        return None
    volume = sum((b.volume for b in session), ZERO)
    notional = sum((b.notional for b in session), ZERO)
    first = session[0].open
    bucket = first * config.profile_bucket_bp / BP
    profile = volume_profile(session, bucket, config.value_area_fraction)
    close = session[-1].close
    return SessionSummary(
        date=market.date,
        open=first,
        high=max(b.high for b in session),
        low=min(b.low for b in session),
        close=close,
        vwap=notional / volume if volume > 0 else close,
        poc=profile.poc if profile else close,
        volume=volume,
        minutes=int((market.close - market.open) / MINUTE),
    )


def ticker_profile(
    history: Sequence[SessionSummary], price: Decimal, config: FeaturesConfig
) -> TickerProfile | None:
    """``history`` oldest first. ``None`` without at least two prior sessions."""
    if len(history) < 2:
        return None
    count = len(history)
    efficiency = sum((h.efficiency for h in history), ZERO) / count
    if efficiency >= config.trend_efficiency:
        character = TickerCharacter.TRENDING
    elif efficiency < config.trend_efficiency / 2:
        character = TickerCharacter.CONSOLIDATING
    else:
        character = TickerCharacter.MIXED
    five_ago = history[-5].close if count >= 5 else None
    return TickerProfile(
        sessions=count,
        return_1d_bp=bp(price - history[-1].close, history[-1].close),
        return_5d_bp=None if five_ago is None else bp(price - five_ago, five_ago),
        average_range_bp=sum((h.range_bp for h in history), ZERO) / count,
        average_efficiency=efficiency,
        up_sessions=sum(1 for h in history if h.close > h.open),
        character=character,
    )


def key_levels(
    session: SessionFeatures | None, history: Sequence[SessionSummary]
) -> tuple[KeyLevel, ...]:
    """Support and resistance from the last session and today so far."""
    levels: list[KeyLevel] = []
    if history:
        last = history[-1]
        levels += [
            KeyLevel("prior_high", last.high),
            KeyLevel("prior_low", last.low),
            KeyLevel("prior_close", last.close),
            KeyLevel("prior_poc", last.poc),
        ]
    if session is not None and session.opening_range_complete:
        if session.opening_range_high is not None:
            levels.append(KeyLevel("opening_range_high", session.opening_range_high))
        if session.opening_range_low is not None:
            levels.append(KeyLevel("opening_range_low", session.opening_range_low))
    return tuple(levels)
