"""Trend following: ride a calm, steady session trend from a pullback to VWAP.

Owner: calm tickers that show consistent strength through the session.

Trigger: a trend session that is not in expansion, with at least
``min_efficiency`` net move, at least ``min_vwap_side_share`` of bars closing
on the trend side of VWAP, and price pulled back to within
``max_pullback_ranges`` × the 15-minute range of VWAP, still on the trend side.
Consistent weakness is traded short the same way. Entry: limit at the current
price. Stop: ``stop_beyond_vwap_ranges`` × the 15-minute range through VWAP,
at least the volatility floor. No target: a runner, managed by its exit plan.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

from perps_agent.risk import Direction
from perps_agent.strategy.playbooks.base import (
    TRENDS,
    Applicability,
    Playbook,
    PlaybookContext,
    PlaybookKind,
    PlaybookParams,
    Setup,
)
from perps_agent.strategy.regime import StructureRegime, VolatilityRegime
from perps_agent.strategy.snapshot import FeatureSnapshot


class TrendFollowingParams(PlaybookParams):
    min_session_minutes: Annotated[int, Field(ge=15, le=150)] = 45
    min_efficiency: Annotated[Decimal, Field(ge=Decimal("0.3"), le=Decimal("0.9"))] = Decimal("0.5")
    min_vwap_side_share: Annotated[
        Decimal, Field(ge=Decimal("0.6"), le=1, description="bars closing on the trend side")
    ] = Decimal("0.8")
    max_pullback_ranges: Annotated[
        Decimal, Field(ge=0, le=2, description="× 15-minute range from VWAP")
    ] = Decimal("0.5")
    stop_beyond_vwap_ranges: Annotated[Decimal, Field(ge=0, le=2)] = Decimal("0.5")
    max_hold_minutes: Annotated[int, Field(ge=1, le=120)] = 120


class TrendFollowing(Playbook[TrendFollowingParams]):
    id = "trend_following"
    logic_version = 1
    kind = PlaybookKind.INTRADAY
    regimes = (
        Applicability(
            volatility=frozenset({VolatilityRegime.COMPRESSED, VolatilityRegime.NORMAL}),
            structure=TRENDS,
        ),
    )
    params_type = TrendFollowingParams
    description = "Buy pullbacks to VWAP in a calm, consistent trend (short the mirror); runner."

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        p = self.params
        session = features.session
        range_15m = features.volatility.range_15m
        assert session is not None and range_15m is not None
        if session.minutes_elapsed < p.min_session_minutes:
            return None
        if session.efficiency < p.min_efficiency:
            return None
        up = features.regime.structure is StructureRegime.TREND_UP
        share = session.share_above_vwap if up else 1 - session.share_above_vwap
        if share < p.min_vwap_side_share:
            return None
        sign = 1 if up else -1
        pullback = (features.price - session.vwap) * sign
        if not 0 < pullback <= p.max_pullback_ranges * range_15m:
            return None
        direction = Direction.LONG if up else Direction.SHORT
        return self._setup(
            features,
            context,
            direction,
            structural_stop=session.vwap - sign * p.stop_beyond_vwap_ranges * range_15m,
            hold_minutes=p.max_hold_minutes,
            reasons=(
                f"{features.regime.structure}: efficiency {session.efficiency:.2f}, "
                f"{share:.0%} of bars on the trend side of VWAP",
                f"pulled back to {session.price_vs_vwap_bp:+.0f} bp from VWAP",
            ),
        )
