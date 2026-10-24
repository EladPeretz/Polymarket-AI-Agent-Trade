"""Momentum scalp: join a breakout in a trending session, aggressively.

Owner: trending sessions, not compressed price action. Scalp, 1–15 minutes;
marketable entry, since getting in needs aggression.

Trigger: the session is trending, the last completed bar closed beyond the
high (low) of the ``breakout_lookback`` bars before it, on at least
``volume_multiple`` × their average volume, and price is on the trend side of
VWAP. Stop: beyond the breakout bar, at least the volatility floor. Target:
``target_r`` × the risk.
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
    average_volume,
)
from perps_agent.strategy.regime import StructureRegime, VolatilityRegime
from perps_agent.strategy.snapshot import FeatureSnapshot


class MomentumScalpParams(PlaybookParams):
    breakout_lookback: Annotated[
        int, Field(ge=3, le=30, description="bars the breakout clears")
    ] = 10
    volume_multiple: Annotated[
        Decimal, Field(ge=1, le=4, description="breakout bar volume × lookback average")
    ] = Decimal("1.5")
    target_r: Annotated[Decimal, Field(ge=Decimal("2.1"), le=4, description="R, net of costs")] = (
        Decimal("2.5")
    )
    max_hold_minutes: Annotated[int, Field(ge=1, le=15)] = 10


class MomentumScalp(Playbook[MomentumScalpParams]):
    id = "momentum_scalp"
    logic_version = 1
    kind = PlaybookKind.SCALP
    regimes = (
        Applicability(
            volatility=frozenset({VolatilityRegime.NORMAL, VolatilityRegime.EXPANSION}),
            structure=TRENDS,
        ),
    )
    params_type = MomentumScalpParams
    description = "Join a volume breakout in the trend's direction; marketable entry, R target."

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        p = self.params
        session = features.session
        assert session is not None
        bars = features.completed_bars
        if len(bars) < p.breakout_lookback + 1:
            return None
        last, before = bars[-1], bars[-1 - p.breakout_lookback : -1]
        if last.volume < p.volume_multiple * average_volume(before):
            return None
        if features.regime.structure is StructureRegime.TREND_UP:
            direction = Direction.LONG
            broke = last.close > max(b.high for b in before)
            with_vwap = features.price > session.vwap
            stop = last.low
        else:
            direction = Direction.SHORT
            broke = last.close < min(b.low for b in before)
            with_vwap = features.price < session.vwap
            stop = last.high
        if not (broke and with_vwap):
            return None
        # Thin symbols often have no trades in the lookback (mark-only bars).
        average = average_volume(before)
        volume = (
            f"breakout volume {last.volume / average:.1f}× average"
            if average > 0
            else "breakout volume after minutes with none"
        )
        return self._setup(
            features,
            context,
            direction,
            structural_stop=stop,
            target_r=p.target_r,
            marketable=True,
            hold_minutes=p.max_hold_minutes,
            reasons=(
                f"{features.regime.structure} session, breakout of {p.breakout_lookback} bars",
                volume,
            ),
        )
