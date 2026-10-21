"""Session regime labels (overview §3.4, ADR-0008).

Two independent labels, each ``None`` until it can be judged:

* **Volatility**: the session's range so far against the symbol's usual
  session range scaled to the time elapsed (``√elapsed / session``, as a
  random walk's range grows). At least ``expansion_ratio`` is *expansion*, at
  most ``compressed_ratio`` *compressed*, otherwise *normal*.
* **Structure**: a *trend* moves at least ``trend_efficiency`` of its range
  net from the open and crosses VWAP at most ``max_trend_vwap_crosses`` times;
  anything else is a *rotation*.

Every trade and backtest result is tagged with these labels, and each
playbook declares which of them it is built for.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from perps_agent.config.models import FeaturesConfig
from perps_agent.strategy.features import SessionFeatures, SessionSummary


class VolatilityRegime(StrEnum):
    EXPANSION = "expansion"
    NORMAL = "normal"
    COMPRESSED = "compressed"


class StructureRegime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    ROTATION = "rotation"

    @property
    def is_trend(self) -> bool:
        return self is not StructureRegime.ROTATION


@dataclass(frozen=True, slots=True)
class Regime:
    volatility: VolatilityRegime | None
    structure: StructureRegime | None
    # Session range so far / the usual range for the time elapsed.
    range_ratio: Decimal | None

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(str(label) for label in (self.volatility, self.structure) if label)


UNKNOWN = Regime(volatility=None, structure=None, range_ratio=None)


def classify(
    session: SessionFeatures | None,
    history: Sequence[SessionSummary],
    config: FeaturesConfig,
) -> Regime:
    if session is None or session.minutes_elapsed < config.regime_min_minutes:
        return UNKNOWN

    if session.efficiency >= config.trend_efficiency and (
        session.vwap_crosses <= config.max_trend_vwap_crosses
    ):
        up = session.price_vs_open_bp > 0
        structure = StructureRegime.TREND_UP if up else StructureRegime.TREND_DOWN
    else:
        structure = StructureRegime.ROTATION

    ratio: Decimal | None = None
    volatility: VolatilityRegime | None = None
    usual = [h for h in history if h.minutes > 0 and h.range_bp > 0]
    if usual:
        average = sum((h.range_bp for h in usual), Decimal(0)) / len(usual)
        minutes = Decimal(sum(h.minutes for h in usual)) / len(usual)
        share = min(Decimal(1), session.minutes_elapsed / minutes)
        ratio = session.range_bp / (average * share.sqrt())
        if ratio >= config.expansion_ratio:
            volatility = VolatilityRegime.EXPANSION
        elif ratio <= config.compressed_ratio:
            volatility = VolatilityRegime.COMPRESSED
        else:
            volatility = VolatilityRegime.NORMAL
    return Regime(volatility=volatility, structure=structure, range_ratio=ratio)
