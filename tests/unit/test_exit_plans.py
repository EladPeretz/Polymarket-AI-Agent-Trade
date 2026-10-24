from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from perps_agent.execution.plans import ExitPlan, ScaleOut, TrailingRule, TrailKind, plan_step
from perps_agent.ledger import LedgerPosition
from perps_agent.risk import AutonomyLevel, Direction
from tests.unit.agent_factories import COST_RATE, AgentDesk, calm_features
from tests.unit.risk_factories import NOW, intent
from tests.unit.strategy_factories import features, minutes, path

D = Decimal
SYMBOL = "NVDA-USD"


def _position(direction: Direction = Direction.LONG, entry: str = "200") -> LedgerPosition:
    stop = D(entry) - direction.sign * 1
    return LedgerPosition(
        position_id="p1",
        symbol=SYMBOL,
        instrument_id=19,
        direction=direction,
        opened_at=NOW,
        level=AutonomyLevel.L2,
        playbook=None,
        approved_risk=D(3),
        planned_stop=stop,
        planned_target=None,
        time_stop=None,
        quantity=D(2),
        average_entry=D(entry),
        stop_price=stop,
    )


def _step(plan: ExitPlan, price: str, position: LedgerPosition | None = None, done=()):
    return plan_step(
        plan,
        position or _position(),
        calm_features(price),
        r_unit=D(1),
        done=frozenset(done),
        cost_rate=COST_RATE,
    )


# --- Vocabulary ---


def test_rules_need_their_parameter():
    with pytest.raises(ValidationError, match="distance_bp"):
        TrailingRule(kind=TrailKind.FIXED)
    with pytest.raises(ValidationError, match="lookback_minutes"):
        TrailingRule(kind=TrailKind.STRUCTURE)
    TrailingRule(kind=TrailKind.VWAP)


def test_scale_outs_must_increase():
    with pytest.raises(ValidationError, match="increasing"):
        ExitPlan(
            scale_outs=(
                ScaleOut(at_r=D(2), fraction=D("0.5")),
                ScaleOut(at_r=D(1), fraction=D(1) / 2),
            )
        )


def test_describe():
    plan = ExitPlan(
        scale_outs=(ScaleOut(at_r=D(1), fraction=D("0.5")),),
        breakeven_after_r=D(1),
        trailing=TrailingRule(kind=TrailKind.FIXED, distance_bp=D(30), activate_after_r=D(1)),
    )
    assert plan.describe() == "scale 0.5 at 1R; breakeven at 1R; trail fixed 30 bp, from 1R"


# --- Evaluation ---


def test_breakeven_moves_stop_to_entry_plus_costs_once_in_profit():
    plan = ExitPlan(breakeven_after_r=D(1))
    assert _step(plan, "200.5").stop is None
    step = _step(plan, "201")
    assert step.stop == D(200) * (1 + COST_RATE)


def test_fixed_trail_follows_price_and_waits_for_activation():
    rule = TrailingRule(kind=TrailKind.FIXED, distance_bp=D(50), activate_after_r=D("0.5"))
    assert _step(ExitPlan(trailing=rule), "200.2").stop is None
    assert _step(ExitPlan(trailing=rule), "202").stop == D(202) - D(202) * D(50) / 10_000


def test_short_trail_is_above_price():
    rule = TrailingRule(kind=TrailKind.FIXED, distance_bp=D(50))
    step = _step(ExitPlan(trailing=rule), "198", _position(Direction.SHORT))
    assert step.stop == D(198) + D(198) * D(50) / 10_000


def test_structure_trail_uses_the_swing_low_of_completed_bars():
    bars = path(NOW - minutes(60), [200] * 55 + [200.5, 201, 201.5, 202, 202.5])
    snapshot = features(bars, at=NOW)
    rule = TrailingRule(kind=TrailKind.STRUCTURE, lookback_minutes=3, offset_bp=D(0))
    step = plan_step(
        ExitPlan(trailing=rule),
        _position(),
        snapshot,
        r_unit=D(1),
        done=frozenset(),
        cost_rate=COST_RATE,
    )
    # Lows of the last three bars: 200.95, 201.45, 201.95 (0.05 wicks).
    assert step.stop == D("200.95")


def test_the_most_protective_candidate_wins_and_never_crosses_price():
    plan = ExitPlan(
        breakeven_after_r=D(1),
        trailing=TrailingRule(kind=TrailKind.FIXED, distance_bp=D(10)),
    )
    step = _step(plan, "202")
    assert step.stop == D(202) - D(202) * D(10) / 10_000
    # A vwap trail at 200 while price is under it would be through the price.
    vwap = ExitPlan(trailing=TrailingRule(kind=TrailKind.VWAP))
    assert _step(vwap, "199.5").stop is None


def test_scale_outs_fire_in_order_once():
    plan = ExitPlan(
        scale_outs=(ScaleOut(at_r=D(1), fraction=D("0.5")), ScaleOut(at_r=D(2), fraction=D("0.5")))
    )
    assert _step(plan, "200.5").reduce is None
    assert _step(plan, "202.5").reduce == (0, D("0.5"))
    assert _step(plan, "202.5", done=[0]).reduce == (1, D("0.5"))
    assert _step(plan, "202.5", done=[0, 1]).reduce is None


# --- Manager on a paper desk ---


def _long(desk: AgentDesk) -> None:
    desk.market(0)
    desk.enter(intent(entry=None, stop="199", level=AutonomyLevel.L2))
    desk.market(300)


def test_manager_tightens_the_stop_and_scales_out():
    desk = AgentDesk()
    _long(desk)
    desk.plans.set_plan(
        SYMBOL,
        ExitPlan(
            scale_outs=(ScaleOut(at_r=D(1), fraction=D("0.5")),),
            breakeven_after_r=D(1),
        ),
    )
    before = desk.ledger.open_position(SYMBOL)
    assert before is not None
    quantity = before.quantity
    desk.market(1_000, mark="201.5", bid="201.48", ask="201.52")
    done = desk.run(desk.plans.on_features(calm_features("201.5")))
    assert any("stop moved" in d for d in done)
    assert any("scaled out" in d for d in done)
    desk.market(1_500, mark="201.5", bid="201.48", ask="201.52")
    position = desk.ledger.open_position(SYMBOL)
    assert position is not None
    assert position.stop_price is not None and position.stop_price > D(200)
    assert position.quantity < quantity
    # The scale-out does not fire again.
    again = desk.run(desk.plans.on_features(calm_features("201.6")))
    assert not any("scaled out" in d for d in again)


def test_manager_rate_limits_stop_moves():
    desk = AgentDesk()
    _long(desk)
    desk.plans.set_plan(
        SYMBOL, ExitPlan(trailing=TrailingRule(kind=TrailKind.FIXED, distance_bp=D(30)))
    )
    desk.market(1_000, mark="201", bid="200.98", ask="201.02")
    assert desk.run(desk.plans.on_features(calm_features("201")))
    desk.market(2_000, mark="201.5", bid="201.48", ask="201.52")
    assert desk.run(desk.plans.on_features(calm_features("201.5"))) == []
    desk.clock.now += timedelta(seconds=5)
    assert desk.run(desk.plans.on_features(calm_features("201.5")))


def test_plan_set_before_fill_attaches_and_is_dropped_when_flat():
    desk = AgentDesk()
    desk.plans.set_plan(SYMBOL, ExitPlan(breakeven_after_r=D(1)))
    _long(desk)
    desk.market(1_000, mark="201.5", bid="201.48", ask="201.52")
    assert desk.run(desk.plans.on_features(calm_features("201.5")))
    desk.run(desk.execution.close_position(SYMBOL, "test"))
    desk.market(2_000, mark="201.5", bid="201.48", ask="201.52")
    assert desk.ledger.open_position(SYMBOL) is None
    assert desk.run(desk.plans.on_features(calm_features("201.5"))) == []
    assert desk.plans.plan(SYMBOL) is None
