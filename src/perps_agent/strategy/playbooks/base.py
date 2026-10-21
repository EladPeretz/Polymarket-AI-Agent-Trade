"""Playbook framework (overview §3.4, ADR-0003, ADR-0008, ADR-0010).

A playbook is a coded, parameterized setup: a pure function from a
:class:`FeatureSnapshot` to a :class:`Setup` (a suggested trade) or ``None``.
No LLM is involved, so playbooks backtest exactly as they run.

* **Regimes:** each playbook declares the regimes it is built for. It stays
  quiet outside them, and evaluation judges it only inside them (ADR-0008).
* **Parameters and bounds:** each playbook's parameters are a pydantic model
  whose field limits are the bounds the learning loop may tune within. They
  live here, in ``main``, so changing a bound is an owner-reviewed PR
  (ADR-0010). Loading parameters outside the bounds raises.
* **Versions:** a playbook's tag is ``<id>@<logic version>.<parameter hash>``,
  so any logic or parameter change is a new version, and results are
  attributed per version.
* **Stops:** a setup's stop is never inside the risk engine's volatility stop
  floor (``risk.stop_floor``), so the playbook does not suggest trades the
  engine would reject for that. The engine still checks every intent.

Setups are suggestions. The agent decides whether to take one, how big, and may
tighten stops or targets; the risk engine decides what is allowed.
"""

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from annotated_types import Ge, Gt, Le, Lt
from pydantic import BaseModel, ConfigDict

from perps_agent.calendar import TradingWindow
from perps_agent.config.models import RiskConfig
from perps_agent.risk import Direction, FeeRates, stop_floor
from perps_agent.strategy.bars import Bar
from perps_agent.strategy.features import BP
from perps_agent.strategy.regime import Regime, StructureRegime, VolatilityRegime
from perps_agent.strategy.snapshot import FeatureSnapshot

# Extra distance beyond the volatility floor, so that rounding a limit entry
# passively onto the price grid cannot pull the stop inside it.
FLOOR_MARGIN_BP = Decimal(1)


class PlaybookKind(StrEnum):
    """Holding time class (owner decision): scalps 1–15 minutes, others 1 minute–2 hours."""

    SCALP = "scalp"
    INTRADAY = "intraday"


MAX_HOLD_MINUTES = {PlaybookKind.SCALP: 15, PlaybookKind.INTRADAY: 120}


class Conviction(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    # Take only with care, at most ``Setup.max_size`` of the budget.
    CAUTION = "caution"


class PlaybookParams(BaseModel):
    """Base for parameter models. Field limits (``ge``/``le``) are the tuning bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class ParameterBound:
    name: str
    default: Any
    minimum: Any
    maximum: Any
    description: str | None


def parameter_bounds(params_type: type[PlaybookParams]) -> tuple[ParameterBound, ...]:
    """Every tunable parameter with its default and bounds, for review and the agent."""
    bounds = []
    for name, field in params_type.model_fields.items():
        low = high = None
        for meta in field.metadata:
            if isinstance(meta, Ge | Gt):
                low = meta.ge if isinstance(meta, Ge) else meta.gt
            elif isinstance(meta, Le | Lt):
                high = meta.le if isinstance(meta, Le) else meta.lt
        if low is None or high is None:
            raise TypeError(f"{params_type.__name__}.{name} needs both bounds")
        bounds.append(ParameterBound(name, field.default, low, high, field.description))
    return tuple(bounds)


@dataclass(frozen=True, slots=True)
class Applicability:
    """One regime combination a playbook is built for. ``None`` accepts any label."""

    volatility: frozenset[VolatilityRegime] | None = None
    structure: frozenset[StructureRegime] | None = None

    def matches(self, regime: Regime) -> bool:
        # A required label that is not known yet does not match.
        if self.volatility is not None and regime.volatility not in self.volatility:
            return False
        return self.structure is None or regime.structure in self.structure


ANY_REGIME = Applicability()
TRENDS = frozenset({StructureRegime.TREND_UP, StructureRegime.TREND_DOWN})


@dataclass(frozen=True, slots=True)
class PlaybookContext:
    """What a playbook needs besides features: the day's window, risk rules and fees."""

    window: TradingWindow
    risk: RiskConfig
    fees: FeeRates

    @property
    def entry_fee_rate(self) -> Decimal:
        # Setups are not post-only, so the risk engine assumes the taker rate.
        return self.fees.taker

    @property
    def exit_cost_rate(self) -> Decimal:
        """A stop or target exit is a market order: taker fee plus the assumed slippage."""
        return self.fees.taker + self.risk.stop_slippage_bp / BP

    def stop_floor(self, entry: Decimal, range_15m: Decimal) -> Decimal:
        assert self.risk.min_stop_distance_bp is not None
        floor = stop_floor(
            entry,
            range_15m,
            min_distance_bp=self.risk.min_stop_distance_bp,
            range_multiple=self.risk.stop_floor_range_multiple,
        )
        return floor + entry * FLOOR_MARGIN_BP / BP


@dataclass(frozen=True, slots=True)
class Setup:
    """A playbook's suggested trade. ``entry_price`` ``None`` means marketable."""

    playbook: str
    symbol: str
    direction: Direction
    reference_price: Decimal
    entry_price: Decimal | None
    stop_price: Decimal
    target_price: Decimal | None
    time_stop: datetime
    conviction: Conviction
    # The most of the per-trade budget this setup should use (the agent sizes within it).
    max_size: Decimal
    # Reward to risk with costs on both sides, as the risk engine measures it.
    reward_risk: Decimal | None
    reasons: tuple[str, ...]
    variant: str | None = None


class Playbook[P: PlaybookParams](ABC):
    id: ClassVar[str]
    # Bump on any change to the trigger, stop or target logic.
    logic_version: ClassVar[int]
    kind: ClassVar[PlaybookKind]
    regimes: ClassVar[tuple[Applicability, ...]]
    long_only: ClassVar[bool] = False
    params_type: ClassVar[type[PlaybookParams]]
    description: ClassVar[str]

    def __init__(self, params: P | None = None) -> None:
        self.params: P = params if params is not None else self.params_type()  # type: ignore[assignment]
        if not isinstance(self.params, self.params_type):
            raise TypeError(f"{self.id} takes {self.params_type.__name__}")

    @property
    def tag(self) -> str:
        return f"{self.id}@{self.logic_version}.{self.params.fingerprint()}"

    def applies(self, regime: Regime) -> bool:
        return any(a.matches(regime) for a in self.regimes)

    def evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        """The setup this playbook sees now, or ``None``.

        Quiet outside the entry window, without a 15-minute range (the stop
        floor would be unknown) and, for most playbooks, outside their regimes.
        """
        window = context.window
        if not window.start <= features.at < window.entry_cutoff:
            return None
        if features.volatility.range_15m is None or features.session is None:
            return None
        if not self.applies(features.regime):
            return None
        setup = self._evaluate(features, context)
        if setup is None or (self.long_only and setup.direction is Direction.SHORT):
            return None
        return setup

    @abstractmethod
    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None: ...

    # --- Helpers for subclasses ---

    def _setup(
        self,
        features: FeatureSnapshot,
        context: PlaybookContext,
        direction: Direction,
        *,
        structural_stop: Decimal,
        target: Decimal | None = None,
        target_r: Decimal | None = None,
        min_target_r: Decimal | None = None,
        marketable: bool = False,
        hold_minutes: int,
        conviction: Conviction = Conviction.NORMAL,
        max_size: Decimal = Decimal(1),
        reasons: Sequence[str],
        variant: str | None = None,
    ) -> Setup | None:
        """Build a setup with the stop pushed out to the floor and the target checked.

        ``target`` is a structural target (VWAP, POC); ``target_r`` sets it at
        that reward to risk instead. Both are measured after costs, as the risk
        engine does. With ``min_target_r`` a closer target drops the setup.
        """
        price = features.price
        sign = direction.sign
        range_15m = features.volatility.range_15m
        assert range_15m is not None
        distance = max((price - structural_stop) * sign, context.stop_floor(price, range_15m))
        stop = price - sign * distance
        if stop <= 0:
            return None
        entry_fee, exit_cost = context.entry_fee_rate, context.exit_cost_rate
        risk = distance + price * entry_fee + stop * exit_cost
        if target_r is not None:
            reach = (target_r * risk + price * (entry_fee + exit_cost)) / (1 - sign * exit_cost)
            target = price + sign * reach
        reward_risk = None
        if target is not None:
            if (target - price) * sign <= 0:
                return None
            reward = abs(target - price) - price * entry_fee - target * exit_cost
            reward_risk = reward / risk
            if min_target_r is not None and reward_risk < min_target_r:
                return None
        hold = min(hold_minutes, MAX_HOLD_MINUTES[self.kind])
        time_stop = min(features.at + timedelta(minutes=hold), context.window.end)
        return Setup(
            playbook=self.tag,
            symbol=features.symbol,
            direction=direction,
            reference_price=price,
            entry_price=None if marketable else price,
            stop_price=stop,
            target_price=target,
            time_stop=time_stop,
            conviction=conviction,
            max_size=max_size,
            reward_risk=reward_risk,
            reasons=tuple(reasons),
            variant=variant,
        )


def average_volume(bars: Sequence[Bar]) -> Decimal:
    return sum((b.volume for b in bars), Decimal(0)) / len(bars) if bars else Decimal(0)
