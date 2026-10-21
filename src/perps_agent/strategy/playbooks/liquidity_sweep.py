"""Liquidity sweep: price runs a key level and snaps straight back.

Owner: any session with a significant support or resistance level from the
last or current session. Price crosses the level and sweeps right back in;
higher conviction when the sweep comes on high volume.

Owner refinement (2026-10-21): only a quick snap back counts, and price must
then fail to break out again for 5 minutes (works especially well on MSTR).

Levels: the prior session's high, low, close and POC, today's opening range
(once complete), and today's high and low from before the sweep. Trigger:

1. **Sweep:** a bar pierced a level by at least ``min_pierce_bp`` while price
   had been on the other side before;
2. **Snap back:** a bar within ``reclaim_within_bars`` of the pierce (the
   sweep bar itself counts) closed back on the original side;
3. **Hold:** for at least ``hold_minutes`` after that, no bar closed beyond the
   level again and none traded past the sweep's extreme.

The setup stays valid for ``entry_window_minutes`` after the hold is complete.
Entry: limit at the current price. Stop: beyond the sweep's extreme, at least
the volatility floor. Target: ``target_r`` × the risk. When several levels
qualify, the most recent confirmation, then the deepest pierce, wins.
"""

from collections.abc import Sequence
from dataclasses import dataclass
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
    min_pierce_bp: Annotated[Decimal, Field(ge=1, le=30, description="how far past the level")] = (
        Decimal(3)
    )
    reclaim_within_bars: Annotated[
        int, Field(ge=1, le=5, description="bars from the pierce to the close back inside")
    ] = 2
    hold_minutes: Annotated[
        int, Field(ge=2, le=15, description="minutes price must stay back inside")
    ] = 5
    entry_window_minutes: Annotated[
        int, Field(ge=1, le=15, description="minutes the setup stays valid after the hold")
    ] = 5
    high_volume_multiple: Annotated[
        Decimal, Field(ge=Decimal("1.2"), le=5, description="sweep volume × session average")
    ] = Decimal(2)
    target_r: Annotated[Decimal, Field(ge=Decimal("2.1"), le=5, description="R, net of costs")] = (
        Decimal("2.5")
    )
    max_hold_minutes: Annotated[int, Field(ge=1, le=120)] = 60


@dataclass(frozen=True, slots=True)
class _Sweep:
    direction: Direction
    level: KeyLevel
    bar: Bar
    depth: Decimal
    held_minutes: int

    @property
    def extreme(self) -> Decimal:
        return self.bar.low if self.direction is Direction.LONG else self.bar.high


class LiquiditySweep(Playbook[LiquiditySweepParams]):
    id = "liquidity_sweep"
    logic_version = 1
    kind = PlaybookKind.INTRADAY
    regimes = (ANY_REGIME,)
    params_type = LiquiditySweepParams
    description = "Trade a quick reclaim of a swept key level that then holds for 5 minutes."

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        p = self.params
        session = features.session
        assert session is not None
        bars = [b for b in features.completed_bars if b.start >= session.started_at]
        sweep = None
        for held in range(p.hold_minutes, p.hold_minutes + p.entry_window_minutes):
            sweep = self._find(bars, features.levels, held)
            if sweep is not None:
                break
        if sweep is None:
            return None
        volume_ratio = sweep.bar.volume / (average_volume(bars) or Decimal(1))
        high_volume = volume_ratio >= p.high_volume_multiple
        return self._setup(
            features,
            context,
            sweep.direction,
            structural_stop=sweep.extreme,
            target_r=p.target_r,
            hold_minutes=p.max_hold_minutes,
            conviction=Conviction.HIGH if high_volume else Conviction.NORMAL,
            reasons=(
                f"swept {sweep.level.name} {sweep.level.price:.2f} to {sweep.extreme}, "
                f"snapped back and held {sweep.held_minutes} min",
                f"sweep bar volume {volume_ratio:.1f}× the session average",
            ),
            variant=sweep.level.name,
        )

    def _find(
        self, bars: Sequence[Bar], key_levels: Sequence[KeyLevel], held: int
    ) -> _Sweep | None:
        """A sweep whose reclaim bar is followed by exactly ``held`` bars that held."""
        p = self.params
        reclaim = len(bars) - 1 - held
        first = reclaim - p.reclaim_within_bars + 1
        if first < 1:
            return None
        before, window, after = bars[:first], bars[first : reclaim + 1], bars[reclaim + 1 :]
        levels = list(key_levels) + [
            KeyLevel("session_high", max(b.high for b in before)),
            KeyLevel("session_low", min(b.low for b in before)),
        ]
        prior_close = before[-1].close
        closed_back = bars[reclaim].close
        low_bar = min(window, key=lambda b: b.low)
        high_bar = max(window, key=lambda b: b.high)
        best: _Sweep | None = None
        for level in levels:
            price = level.price
            pierce = price * p.min_pierce_bp / BP
            candidates = []
            # Support swept and reclaimed, then no close below it or new low.
            if prior_close > price and closed_back > price:
                if all(b.close > price and b.low > low_bar.low for b in after):
                    candidates.append(
                        _Sweep(Direction.LONG, level, low_bar, price - low_bar.low, held)
                    )
            # Resistance swept and reclaimed, then no close above it or new high.
            if prior_close < price and closed_back < price:
                if all(b.close < price and b.high < high_bar.high for b in after):
                    candidates.append(
                        _Sweep(Direction.SHORT, level, high_bar, high_bar.high - price, held)
                    )
            for candidate in candidates:
                if candidate.depth >= pierce and (best is None or candidate.depth > best.depth):
                    best = candidate
        return best
