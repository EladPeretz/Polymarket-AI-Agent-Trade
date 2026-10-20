"""Mean reversion: fade the edges of a compressed session, or a breakout against the trend.

Owner: compressed sessions, or fading recent breakouts that go against the
trend; breakout fades only with caution.

* ``value_area`` variant (compressed sessions only): price is outside the
  session's value area and the last completed bar turned back inside it.
* ``breakout_fade`` variant (any session, caution, at most ``fade_max_size``):
  price broke the opening range against the ticker's recent trend (its 5-session
  return) and the last completed bar closed back inside the range.

Entry: limit at the current price. Stop: beyond the extreme of the last
``stop_lookback`` bars, at least the volatility floor. Target: the session POC,
at least ``min_target_r`` away.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field

from perps_agent.risk import Direction
from perps_agent.strategy.playbooks.base import (
    ANY_REGIME,
    Conviction,
    Playbook,
    PlaybookContext,
    PlaybookKind,
    PlaybookParams,
    Setup,
)
from perps_agent.strategy.regime import VolatilityRegime
from perps_agent.strategy.snapshot import FeatureSnapshot


class MeanReversionParams(PlaybookParams):
    min_session_minutes: Annotated[int, Field(ge=15, le=120)] = 30
    stop_lookback: Annotated[
        int, Field(ge=3, le=30, description="bars whose extreme the stop clears")
    ] = 10
    min_target_r: Annotated[
        Decimal, Field(ge=Decimal("2.1"), le=5, description="target at least this many R, net")
    ] = Decimal("2.2")
    fade_max_size: Annotated[
        Decimal, Field(ge=Decimal("0.1"), le=1, description="budget cap for breakout fades")
    ] = Decimal("0.5")
    max_hold_minutes: Annotated[int, Field(ge=1, le=120)] = 90


class MeanReversion(Playbook[MeanReversionParams]):
    id = "mean_reversion"
    logic_version = 1
    kind = PlaybookKind.INTRADAY
    # The value-area variant checks for a compressed session itself.
    regimes = (ANY_REGIME,)
    params_type = MeanReversionParams
    description = "Fade value-area edges in compressed sessions, or counter-trend breakouts."

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        p = self.params
        session, profile = features.session, features.profile
        assert session is not None
        if session.minutes_elapsed < p.min_session_minutes or profile is None:
            return None
        bars = features.completed_bars
        if len(bars) < max(2, p.stop_lookback):
            return None
        last, previous = bars[-1], bars[-2]
        window = bars[-p.stop_lookback :]
        high, low = max(b.high for b in window), min(b.low for b in window)

        if features.regime.volatility is VolatilityRegime.COMPRESSED:
            if previous.close > profile.value_area_high >= last.close:
                return self._fade(
                    features,
                    context,
                    Direction.SHORT,
                    high,
                    "value_area",
                    "turned back below the value area high",
                )
            if previous.close < profile.value_area_low <= last.close:
                return self._fade(
                    features,
                    context,
                    Direction.LONG,
                    low,
                    "value_area",
                    "turned back above the value area low",
                )

        ticker = features.ticker
        if not session.opening_range_complete or ticker is None or ticker.return_5d_bp is None:
            return None
        top, bottom = session.opening_range_high, session.opening_range_low
        if top is None or bottom is None:
            return None
        if ticker.return_5d_bp < 0 and high > top >= last.close and previous.close > top:
            return self._fade(
                features,
                context,
                Direction.SHORT,
                high,
                "breakout_fade",
                "upside opening-range break against a falling 5-session trend failed",
            )
        if ticker.return_5d_bp > 0 and low < bottom <= last.close and previous.close < bottom:
            return self._fade(
                features,
                context,
                Direction.LONG,
                low,
                "breakout_fade",
                "downside opening-range break against a rising 5-session trend failed",
            )
        return None

    def _fade(
        self,
        features: FeatureSnapshot,
        context: PlaybookContext,
        direction: Direction,
        extreme: Decimal,
        variant: str,
        reason: str,
    ) -> Setup | None:
        assert features.profile is not None
        caution = variant == "breakout_fade"
        return self._setup(
            features,
            context,
            direction,
            structural_stop=extreme,
            target=features.profile.poc,
            min_target_r=self.params.min_target_r,
            hold_minutes=self.params.max_hold_minutes,
            conviction=Conviction.CAUTION if caution else Conviction.NORMAL,
            max_size=self.params.fade_max_size if caution else Decimal(1),
            reasons=(reason, f"target the session POC {features.profile.poc:.2f}"),
            variant=variant,
        )
