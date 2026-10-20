"""Features, regime labels, playbooks and the trigger detector (overview §3.4).

Pure functions of market data and the calendar, with no I/O except the
history loader, so backtests and live trading compute the same thing.
"""

from perps_agent.strategy.bars import Bar, MinuteBars, bar_from_candle, minute_of
from perps_agent.strategy.features import (
    KeyLevel,
    SessionFeatures,
    SessionSummary,
    TickerCharacter,
    TickerProfile,
    VolatilityFeatures,
    VolumeProfile,
    minute_volatility,
    session_features,
    summarize_session,
    ticker_profile,
    volatility_features,
    volume_profile,
)
from perps_agent.strategy.history import (
    load_bars,
    load_history,
    load_recent_bars,
    prior_sessions,
    summarize_sessions,
)
from perps_agent.strategy.regime import Regime, StructureRegime, VolatilityRegime, classify
from perps_agent.strategy.snapshot import FeatureSnapshot, build_features
from perps_agent.strategy.triggers import Trigger, TriggerDetector, WatchedPosition

__all__ = [
    "Bar",
    "FeatureSnapshot",
    "KeyLevel",
    "MinuteBars",
    "Regime",
    "SessionFeatures",
    "SessionSummary",
    "StructureRegime",
    "TickerCharacter",
    "TickerProfile",
    "Trigger",
    "TriggerDetector",
    "VolatilityFeatures",
    "VolatilityRegime",
    "VolumeProfile",
    "WatchedPosition",
    "bar_from_candle",
    "build_features",
    "classify",
    "load_bars",
    "load_history",
    "load_recent_bars",
    "minute_of",
    "minute_volatility",
    "prior_sessions",
    "session_features",
    "summarize_session",
    "summarize_sessions",
    "ticker_profile",
    "volatility_features",
    "volume_profile",
]
