"""Engine-run exit plans and trailing stops (overview §3.8, ADR-0009).

The agent attaches an :class:`ExitPlan` to a position and may replace it at
any time; the engine evaluates it on every feature update, independently of
when the agent wakes. A plan combines:

* **Scale-outs:** at a profit of ``at_r`` (multiples of the initial risk),
  reduce the position by ``fraction`` of what is still open, once each.
* **Breakeven:** at a profit of ``breakeven_after_r``, move the stop to the
  entry plus round-trip costs.
* **Trailing rule**, from a small vocabulary the agent parameterizes:

  - ``fixed``: ``distance_bp`` behind the price;
  - ``volatility``: ``range_multiple`` × the recent 15-minute range behind it;
  - ``structure``: beyond the lowest low (long) or highest high (short) of the
    last ``lookback_minutes`` completed 1-minute bars, plus ``offset_bp``;
  - ``vwap``: ``offset_bp`` beyond the session VWAP.

  ``activate_after_r`` delays trailing until the position is that far in profit.

R is measured against the position's initial risk per unit: the distance from
the average entry to the stop the risk engine approved.

Every stop move goes through ``ExecutionEngine.update_protection``, so it is
checked by ``RiskEngine.check_stop_change``: stops only tighten, and a stop at
or through the price is refused. Moves are rate-limited
(``trail_min_interval_seconds``) and must be at least ``trail_min_step_bp``.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from perps_agent.clock import to_utc, utc_now
from perps_agent.config.models import ExecutionConfig
from perps_agent.execution.engine import ExecutionEngine, ExecutionError
from perps_agent.ledger import LedgerPosition
from perps_agent.strategy.snapshot import FeatureSnapshot
from perps_agent.venue import VenueError

log = logging.getLogger(__name__)

BP = Decimal(10_000)
RMultiple = Annotated[Decimal, Field(gt=0, le=20)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrailKind(StrEnum):
    FIXED = "fixed"
    VOLATILITY = "volatility"
    STRUCTURE = "structure"
    VWAP = "vwap"


_REQUIRED = {
    TrailKind.FIXED: "distance_bp",
    TrailKind.VOLATILITY: "range_multiple",
    TrailKind.STRUCTURE: "lookback_minutes",
}


class TrailingRule(_Model):
    kind: TrailKind
    distance_bp: Annotated[Decimal, Field(gt=0, le=1000)] | None = None
    range_multiple: Annotated[Decimal, Field(gt=0, le=10)] | None = None
    lookback_minutes: Annotated[int, Field(ge=1, le=60)] | None = None
    offset_bp: Annotated[Decimal, Field(ge=0, le=500)] = Decimal(0)
    activate_after_r: RMultiple | None = None

    @model_validator(mode="after")
    def _parameters(self) -> Self:
        needed = _REQUIRED.get(self.kind)
        if needed is not None and getattr(self, needed) is None:
            raise ValueError(f"a {self.kind} trailing rule needs {needed}")
        return self

    def describe(self) -> str:
        match self.kind:
            case TrailKind.FIXED:
                text = f"fixed {self.distance_bp} bp"
            case TrailKind.VOLATILITY:
                text = f"{self.range_multiple}x 15-minute range"
            case TrailKind.STRUCTURE:
                text = f"{self.lookback_minutes}-minute swing + {self.offset_bp} bp"
            case TrailKind.VWAP:
                text = f"VWAP + {self.offset_bp} bp"
        if self.activate_after_r is not None:
            text += f", from {self.activate_after_r}R"
        return text


class ScaleOut(_Model):
    at_r: RMultiple
    # Share of the quantity still open when the step fires.
    fraction: Annotated[Decimal, Field(gt=0, lt=1)]


class ExitPlan(_Model):
    scale_outs: tuple[ScaleOut, ...] = ()
    breakeven_after_r: RMultiple | None = None
    trailing: TrailingRule | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        levels = [s.at_r for s in self.scale_outs]
        if levels != sorted(set(levels)):
            raise ValueError("scale-out steps must have increasing, distinct at_r")
        if len(levels) > 5:
            raise ValueError("at most 5 scale-out steps")
        return self

    def describe(self) -> str:
        parts = [f"scale {s.fraction} at {s.at_r}R" for s in self.scale_outs]
        if self.breakeven_after_r is not None:
            parts.append(f"breakeven at {self.breakeven_after_r}R")
        if self.trailing is not None:
            parts.append(f"trail {self.trailing.describe()}")
        return "; ".join(parts) or "no rules"


@dataclass(frozen=True, slots=True)
class PlanStep:
    """What a plan asks for now: a tighter stop and/or a partial exit."""

    stop: Decimal | None = None
    stop_reason: str | None = None
    # (scale-out index, fraction of the open quantity)
    reduce: tuple[int, Decimal] | None = None


def profit_r(position: LedgerPosition, price: Decimal, r_unit: Decimal) -> Decimal:
    """How far ``price`` is in the position's favour, in multiples of the initial risk."""
    if r_unit <= 0:
        return Decimal(0)
    return (price - position.average_entry) * position.direction.sign / r_unit


def plan_step(
    plan: ExitPlan,
    position: LedgerPosition,
    features: FeatureSnapshot,
    *,
    r_unit: Decimal,
    done: frozenset[int],
    cost_rate: Decimal,
) -> PlanStep:
    """The stop and scale-out the plan wants at ``features.price``. Pure.

    ``done`` holds the scale-out steps already taken. The stop returned is the
    most protective candidate; whether it is a real tightening is the caller's
    check. ``cost_rate`` is the round-trip cost added to the breakeven stop.
    """
    price = features.price
    sign = position.direction.sign
    r = profit_r(position, price, r_unit)
    candidates: list[tuple[Decimal, str]] = []
    if plan.breakeven_after_r is not None and r >= plan.breakeven_after_r:
        entry = position.average_entry
        candidates.append((entry + sign * entry * cost_rate, f"breakeven at {r:.2f}R"))
    rule = plan.trailing
    if rule is not None and (rule.activate_after_r is None or r >= rule.activate_after_r):
        level = _trail_level(rule, features, sign)
        if level is not None:
            candidates.append((level, f"trail {rule.describe()}"))
    # Never at or through the price: that is a close, not a stop.
    candidates = [(s, why) for s, why in candidates if (price - s) * sign > 0 and s > 0]
    stop = reason = None
    if candidates:
        stop, reason = max(candidates, key=lambda c: c[0] * sign)
    reduce = None
    for index, step in enumerate(plan.scale_outs):
        if index not in done and r >= step.at_r:
            reduce = (index, step.fraction)
            break
    return PlanStep(stop=stop, stop_reason=reason, reduce=reduce)


def _trail_level(rule: TrailingRule, features: FeatureSnapshot, sign: int) -> Decimal | None:
    price = features.price
    offset = price * rule.offset_bp / BP
    match rule.kind:
        case TrailKind.FIXED:
            assert rule.distance_bp is not None
            return price - sign * price * rule.distance_bp / BP
        case TrailKind.VOLATILITY:
            assert rule.range_multiple is not None
            range_15m = features.volatility.range_15m
            if range_15m is None:
                return None
            return price - sign * rule.range_multiple * range_15m
        case TrailKind.STRUCTURE:
            assert rule.lookback_minutes is not None
            bars = features.completed_bars[-rule.lookback_minutes :]
            if len(bars) < rule.lookback_minutes:
                return None
            if sign > 0:
                return min(b.low for b in bars) - offset
            return max(b.high for b in bars) + offset
        case TrailKind.VWAP:
            if features.session is None:
                return None
            return features.session.vwap - sign * offset


@dataclass(slots=True)
class _Active:
    position_id: str
    plan: ExitPlan
    r_unit: Decimal
    done: set[int] = field(default_factory=set)
    last_move: datetime | None = None


class ExitPlanManager:
    """Holds each position's plan and carries it out through execution."""

    def __init__(
        self,
        execution: ExecutionEngine,
        config: ExecutionConfig,
        *,
        cost_rate: Decimal,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._execution = execution
        self._config = config
        self._cost_rate = cost_rate
        self._clock = clock
        self._plans: dict[str, _Active] = {}  # symbol → active plan

    def set_plan(self, symbol: str, plan: ExitPlan | None) -> None:
        """Attach (or with ``None`` remove) the plan of the symbol's open position.

        A plan set while only an entry is working attaches when it fills.
        """
        if plan is None:
            self._plans.pop(symbol, None)
            return
        position = self._execution.ledger.open_position(symbol)
        position_id = position.position_id if position is not None else ""
        r_unit = _r_unit(position) if position is not None else Decimal(0)
        previous = self._plans.get(symbol)
        done = previous.done if previous and previous.position_id == position_id else set()
        self._plans[symbol] = _Active(position_id, plan, r_unit, done=done)

    def plan(self, symbol: str) -> ExitPlan | None:
        active = self._plans.get(symbol)
        return active.plan if active is not None else None

    async def on_features(self, features: FeatureSnapshot) -> list[str]:
        """Apply the symbol's plan at the new price. Returns what was done, in words."""
        symbol = features.symbol
        active = self._plans.get(symbol)
        if active is None:
            return []
        position = self._execution.ledger.open_position(symbol)
        if position is None:
            if active.position_id:
                del self._plans[symbol]  # the position it belonged to is closed
            return []
        if active.position_id != position.position_id:
            if active.position_id:
                del self._plans[symbol]
                return []
            # Set while the entry was working: it belongs to this position now.
            active.position_id, active.r_unit = position.position_id, _r_unit(position)
        if position.stop_price is None or symbol in self._execution.closing:
            return []
        step = plan_step(
            active.plan,
            position,
            features,
            r_unit=active.r_unit,
            done=frozenset(active.done),
            cost_rate=self._cost_rate,
        )
        done: list[str] = []
        now = to_utc(self._clock())
        if step.stop is not None and self._may_move(active, position, step.stop, now):
            try:
                refused = await self._execution.update_protection(
                    symbol, stop_price=step.stop, reason=step.stop_reason or "exit plan"
                )
            except (ExecutionError, VenueError) as error:
                log.warning("exit plan stop move failed", extra={"error": str(error)})
            else:
                if refused is None:
                    active.last_move = now
                    done.append(f"stop moved to {step.stop:.6g} ({step.stop_reason})")
        if step.reduce is not None:
            index, fraction = step.reduce
            active.done.add(index)  # once, even if the exit fails
            try:
                await self._execution.reduce_position(
                    symbol, fraction, f"exit plan: scale-out {index + 1}"
                )
            except (ExecutionError, VenueError) as error:
                log.warning("exit plan scale-out failed", extra={"error": str(error)})
            else:
                done.append(f"scaled out {fraction} of the position (step {index + 1})")
        return done

    def _may_move(
        self, active: _Active, position: LedgerPosition, stop: Decimal, now: datetime
    ) -> bool:
        current = position.stop_price
        assert current is not None
        step = (stop - current) * position.direction.sign
        if step <= 0 or step < current * self._config.trail_min_step_bp / BP:
            return False
        interval = timedelta(seconds=self._config.trail_min_interval_seconds)
        return active.last_move is None or now - active.last_move >= interval


def _r_unit(position: LedgerPosition) -> Decimal:
    stop = position.planned_stop if position.planned_stop is not None else position.stop_price
    if stop is None:
        return Decimal(0)
    return abs(position.average_entry - stop)
