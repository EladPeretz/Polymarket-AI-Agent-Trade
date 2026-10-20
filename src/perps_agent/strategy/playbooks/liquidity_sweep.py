"""Liquidity sweep: price runs a key level and snaps straight back.

Owner: any session with a significant support or resistance level from the
last or current session. Price crosses the level and sweeps right back in;
higher conviction when the sweep comes on high volume.

Levels: the prior session's high, low, close and POC, today's opening range
(once complete), and today's high and low from before the sweep. Trigger:
within the last ``sweep_window`` completed bars a bar pierced a level by at
least ``min_pierce_bp``, price had been on the other side before, and the last
completed bar closed back there. Entry: limit at the current price. Stop:
beyond the sweep's extreme, at least the volatility floor. Target:
``target_r`` × the risk. When several levels were swept, the deepest pierce wins.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

from perps_agent.risk import Direction
from perps_agent.strategy.bars import Bar
from perps_agent.strategy.features import BP, KeyLevel
from perps_agent.strategy.playbooks.base import (
    ANY_REGIME,
    Conviction,
    Playbook,
    PlaybookContext,
    PlaybookKind,
    PlaybookParams,
    Setup,
    average_volume,
)
from perps_agent.strategy.snapshot import FeatureSnapshot


class LiquiditySweepParams(PlaybookParams):
    sweep_window: Annotated[int, Field(ge=1, le=15, description="bars the sweep happened in")] = 5
    min_pierce_bp: Annotated[Decimal, Field(ge=1, le=30, description="how far past the level")] = (
        Decimal(3)
    )
    high_volume_multiple: Annotated[
        Decimal, Field(ge=Decimal("1.2"), le=5, description="sweep volume × session average")
    ] = Decimal(2)
    target_r: Annotated[Decimal, Field(ge=Decimal("2.1"), le=5, description="R, net of costs")] = (
        Decimal("2.5")
    )
    max_hold_minutes: Annotated[int, Field(ge=1, le=120)] = 60


class LiquiditySweep(Playbook[LiquiditySweepParams]):
    id = "liquidity_sweep"
    logic_version = 1
    kind = PlaybookKind.INTRADAY
    regimes = (ANY_REGIME,)
    params_type = LiquiditySweepParams
    description = "Trade the reclaim after price sweeps a key level; higher conviction on volume."

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        p = self.params
        session = features.session
        assert session is not None
        session_bars = [b for b in features.completed_bars if b.start >= session.started_at]
        if len(session_bars) < p.sweep_window + 2:
            return None
        sweep, before = session_bars[-p.sweep_window :], session_bars[: -p.sweep_window]
        last = session_bars[-1]
        levels = list(features.levels) + [
            KeyLevel("session_high", max(b.high for b in before)),
            KeyLevel("session_low", min(b.low for b in before)),
        ]
        prior_close = before[-1].close
        low_bar = min(sweep, key=lambda b: b.low)
        high_bar = max(sweep, key=lambda b: b.high)
        best: tuple[Decimal, Direction, KeyLevel, Bar] | None = None
        for level in levels:
            pierce = level.price * p.min_pierce_bp / BP
            # Support swept: was above, traded through, closed back above.
            if prior_close > level.price and last.close > level.price:
                depth = level.price - low_bar.low
                if depth >= pierce and (best is None or depth > best[0]):
                    best = (depth, Direction.LONG, level, low_bar)
            # Resistance swept: was below, traded through, closed back below.
            if prior_close < level.price and last.close < level.price:
                depth = high_bar.high - level.price
                if depth >= pierce and (best is None or depth > best[0]):
                    best = (depth, Direction.SHORT, level, high_bar)
        if best is None:
            return None
        _, direction, level, bar = best
        volume_ratio = bar.volume / (average_volume(session_bars) or Decimal(1))
        high_volume = volume_ratio >= p.high_volume_multiple
        extreme = bar.low if direction is Direction.LONG else bar.high
        return self._setup(
            features,
            context,
            direction,
            structural_stop=extreme,
            target_r=p.target_r,
            hold_minutes=p.max_hold_minutes,
            conviction=Conviction.HIGH if high_volume else Conviction.NORMAL,
            reasons=(
                f"swept {level.name} {level.price:.2f} to {extreme} and reclaimed it",
                f"sweep bar volume {volume_ratio:.1f}× the session average",
            ),
            variant=level.name,
        )
