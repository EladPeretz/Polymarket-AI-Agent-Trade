"""VWAP reversion: fade a stretch away from session VWAP once price turns back.

Owner: general, not tied to one regime.

Trigger: price is at least ``stretch_range_60m`` × the 60-minute range from
VWAP, and the last completed bar turned back towards VWAP (closed against its
open and beyond the previous close). Entry: limit at the current price. Stop:
beyond the extreme of the last 15 minutes, at least the volatility floor.
Target: VWAP, at least ``min_target_r`` away.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import Field

from perps_agent.risk import Direction
from perps_agent.strategy.playbooks.base import (
    ANY_REGIME,
    Playbook,
    PlaybookContext,
    PlaybookKind,
    PlaybookParams,
    Setup,
)
from perps_agent.strategy.snapshot import FeatureSnapshot


class VwapReversionParams(PlaybookParams):
    min_session_minutes: Annotated[
        int, Field(ge=5, le=90, description="minutes after the open before it may fire")
    ] = 15
    stretch_range_60m: Annotated[
        Decimal, Field(ge=Decimal("0.25"), le=Decimal("1.5"), description="× 60-minute range")
    ] = Decimal("0.6")
    min_target_r: Annotated[
        Decimal, Field(ge=Decimal("2.1"), le=5, description="target at least this many R, net")
    ] = Decimal("2.2")
    max_hold_minutes: Annotated[int, Field(ge=1, le=120)] = 60


class VwapReversion(Playbook[VwapReversionParams]):
    id = "vwap_reversion"
    logic_version = 1
    kind = PlaybookKind.INTRADAY
    regimes = (ANY_REGIME,)
    params_type = VwapReversionParams
    description = "Fade a stretch from session VWAP after price turns back; target VWAP."

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        p = self.params
        session = features.session
        range_60m = features.volatility.range_60m
        assert session is not None
        if session.minutes_elapsed < p.min_session_minutes or not range_60m:
            return None
        stretch = features.price - session.vwap
        if abs(stretch) < p.stretch_range_60m * range_60m:
            return None
        bars = features.completed_bars
        if len(bars) < 2:
            return None
        last, previous = bars[-1], bars[-2]
        recent = [b for b in bars if b.start >= features.at - timedelta(minutes=15)]
        if not recent:
            return None
        if stretch > 0:
            direction = Direction.SHORT
            turned = last.close < last.open and last.close < previous.close
            extreme = max(b.high for b in recent)
        else:
            direction = Direction.LONG
            turned = last.close > last.open and last.close > previous.close
            extreme = min(b.low for b in recent)
        if not turned:
            return None
        return self._setup(
            features,
            context,
            direction,
            structural_stop=extreme,
            target=session.vwap,
            min_target_r=p.min_target_r,
            hold_minutes=p.max_hold_minutes,
            reasons=(
                f"price {session.price_vs_vwap_bp:+.0f} bp from VWAP {session.vwap:.2f}",
                "last bar turned back towards VWAP",
            ),
        )
