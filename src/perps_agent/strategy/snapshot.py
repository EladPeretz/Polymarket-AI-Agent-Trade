"""The feature snapshot: one symbol's features, levels and regime at one moment."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from perps_agent.calendar import TradingWindow
from perps_agent.clock import to_utc
from perps_agent.config.models import FeaturesConfig
from perps_agent.strategy.bars import Bar
from perps_agent.strategy.features import (
    BP,
    KeyLevel,
    SessionFeatures,
    SessionSummary,
    TickerProfile,
    VolatilityFeatures,
    VolumeProfile,
    key_levels,
    session_features,
    ticker_profile,
    volatility_features,
    volume_profile,
)
from perps_agent.strategy.regime import Regime, classify

# Bars before the session kept in a snapshot, for the volatility windows.
LOOKBACK = timedelta(minutes=60)


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    symbol: str
    at: datetime
    price: Decimal
    volatility: VolatilityFeatures
    session: SessionFeatures | None
    profile: VolumeProfile | None
    levels: tuple[KeyLevel, ...]
    ticker: TickerProfile | None
    regime: Regime
    # Prior sessions, oldest first.
    history: tuple[SessionSummary, ...]
    # Bars from the session open (or the last hour, if earlier), oldest first,
    # for playbook triggers. Not meant for the agent's compact context.
    bars: tuple[Bar, ...]

    @property
    def session_bars(self) -> tuple[Bar, ...]:
        if self.session is None:
            return ()
        start = self.session.started_at
        return tuple(b for b in self.bars if b.start >= start)

    @property
    def completed_bars(self) -> tuple[Bar, ...]:
        """Bars that ended at or before ``at``: the current, partial minute excluded."""
        return tuple(b for b in self.bars if b.end <= self.at)


def build_features(
    symbol: str,
    *,
    bars: Sequence[Bar],
    price: Decimal,
    now: datetime,
    window: TradingWindow | None,
    history: Sequence[SessionSummary],
    config: FeaturesConfig,
) -> FeatureSnapshot:
    """Compute every feature from ``bars`` (oldest first) and prior ``history``.

    ``price`` is the current price (mark). ``window`` is today's trading window,
    ``None`` on a non-trading day.
    """
    now = to_utc(now)
    volatility = volatility_features(bars, price, now, window, config)
    session = None
    profile = None
    start = now - LOOKBACK
    if window is not None:
        market = window.market
        session = session_features(bars, price, now, market, history, config)
        if session is not None:
            start = min(start, market.open)
            todays = [b for b in bars if market.open <= b.start < now]
            bucket = session.open * config.profile_bucket_bp / BP
            profile = volume_profile(todays, bucket, config.value_area_fraction)
    return FeatureSnapshot(
        symbol=symbol,
        at=now,
        price=price,
        volatility=volatility,
        session=session,
        profile=profile,
        levels=key_levels(session, history),
        ticker=ticker_profile(history, price, config),
        regime=classify(session, history, config),
        history=tuple(history),
        bars=tuple(b for b in bars if start <= b.start <= now),
    )
