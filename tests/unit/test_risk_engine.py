from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from perps_agent.config.models import AutonomyConfig, RiskConfig, SymbolConfig
from perps_agent.risk import (
    AutonomyLevel,
    Decision,
    Direction,
    PositionRisk,
    RiskCheck,
    RiskVerdict,
    liquidation_price,
    loss_per_unit,
)
from perps_agent.venue import Side, TimeInForce
from tests.unit.risk_factories import (
    NOW,
    RISK,
    engine,
    intent,
    position,
    snapshot,
    state,
)
from tests.unit.venue_factories import instrument

D = Decimal


def _evaluate(trade=None, *, snap=None, account=None, at=NOW, instr=None, risk=RISK, symbols=None):
    return engine(risk, symbols).evaluate(
        trade or intent(),
        instrument=instr or instrument(),
        snapshot=snap or snapshot(),
        state=account or state(),
        now=at,
    )


def _checks(verdict: RiskVerdict) -> set[RiskCheck]:
    return {reason.check for reason in verdict.reasons}


# --- Approval and sizing ---


def test_limit_long_is_sized_from_the_risk_budget():
    verdict = _evaluate()
    assert verdict.decision is Decision.APPROVED
    assert verdict.reasons == ()
    # 500 × 0.7% = 3.5 pUSD at risk. Each unit loses the 1.00 stop distance, the
    # 4 bp taker entry fee (0.08) and the stop's 4 bp fee + 5 bp slippage on 199
    # (0.1791): 3.5 / 1.2591 = 2.7797.
    assert verdict.risk_budget == D("3.5")
    order = verdict.order
    assert order is not None
    assert order.side is Side.BUY
    assert order.quantity == D("2.7797")
    assert order.price == D(200)
    assert order.time_in_force is TimeInForce.GTC
    assert not order.reduce_only
    assert verdict.risk_amount == D("2.7797") * D("1.2591")
    assert verdict.leverage == 10
    assert verdict.stop_price == D(199)


def test_size_is_a_fraction_of_the_budget_and_rounds_down():
    verdict = _evaluate(intent(size="0.33333"))
    assert verdict.order is not None
    # 3.5 × 0.33333 / 1.2591 = 0.92659 → 4 decimals, rounded down.
    assert verdict.order.quantity == D("0.9265")
    assert verdict.risk_amount is not None and verdict.risk_amount <= verdict.risk_budget


def test_l2_uses_the_reduced_budget():
    verdict = _evaluate(intent(level=AutonomyLevel.L2))
    assert verdict.risk_budget == D("1.75")
    assert verdict.order is not None and verdict.order.quantity == D("1.3898")  # 1.75 / 1.2591


def test_budget_uses_the_lower_of_current_and_day_start_equity():
    up = _evaluate(account=state("600", day_start="500"))
    down = _evaluate(account=state("490", day_start="500"))
    assert up.risk_budget == D("3.5")
    assert down.risk_budget == D("3.43")


def test_short_limit_entry():
    verdict = _evaluate(intent(Direction.SHORT, entry="200", stop="201"))
    assert verdict.decision is Decision.APPROVED
    assert verdict.order is not None
    assert verdict.order.side is Side.SELL
    # 1.00 + 0.08 entry fee + 201 × 9 bp exit cost = 1.2609 per unit.
    assert verdict.order.quantity == D("2.7757")


def test_marketable_entry_is_an_ioc_limited_at_the_worst_level_it_reaches():
    snap = snapshot(asks=(("200.02", "10"), ("200.10", "30")))
    verdict = _evaluate(intent(entry=None, stop="199"), snap=snap, account=state("2000"))
    assert verdict.decision is Decision.APPROVED
    order = verdict.order
    assert order is not None
    assert order.time_in_force is TimeInForce.IOC
    # 14 pUSD / 1.2791 (1.02 + costs) = 10.9 does not fit in the first level (10),
    # so it is priced at 200.10 and re-sized over the longer distance: 14 / 1.3591,
    # to 5 figures.
    assert order.price == D("200.10")
    assert verdict.entry_price == D("200.10")
    assert order.quantity == D("10.300")
    assert verdict.risk_amount is not None and verdict.risk_amount <= D(14)


def test_marketable_short_takes_the_bid():
    verdict = _evaluate(intent(Direction.SHORT, entry=None, stop="201"))
    assert verdict.order is not None
    assert verdict.order.price == D("199.98")
    assert verdict.order.side is Side.SELL


def test_marketable_entry_is_cut_to_the_depth_shown():
    snap = snapshot(asks=(("200.02", "10"), ("200.03", "1")))
    big = state("100000", available="100000")
    risk = RISK.model_copy(update={"max_position_notional": D(1_000_000)})
    verdict = _evaluate(intent(entry=None, stop="199"), snap=snap, account=big, risk=risk)
    assert verdict.decision is Decision.RESIZED
    assert RiskCheck.BOOK_DEPTH in _checks(verdict)
    assert verdict.order is not None and verdict.order.quantity == D(11)


def test_notional_is_capped_by_max_position_notional():
    verdict = _evaluate(account=state("100000"))
    assert verdict.decision is Decision.RESIZED
    assert _checks(verdict) == {RiskCheck.NOTIONAL_CAP}
    assert verdict.order is not None and verdict.order.quantity == D(25)  # 5000 / 200


def test_size_is_cut_to_fit_available_margin():
    verdict = _evaluate(account=state("500", available="20"))
    assert verdict.decision is Decision.RESIZED
    assert _checks(verdict) == {RiskCheck.MARGIN}
    # 20 × 10x leverage / 200 = 1 unit.
    assert verdict.order is not None and verdict.order.quantity == D(1)


def test_too_small_after_sizing_is_rejected():
    verdict = _evaluate(intent(size="0.01"))
    assert verdict.decision is Decision.REJECTED
    assert _checks(verdict) == {RiskCheck.SIZE_TOO_SMALL}
    assert verdict.order is None


def test_order_ids_are_fresh_32_hex():
    risk_engine = engine()
    first = risk_engine.evaluate(
        intent(), instrument=instrument(), snapshot=snapshot(), state=state(), now=NOW
    )
    second = risk_engine.evaluate(
        intent(), instrument=instrument(), snapshot=snapshot(), state=state(), now=NOW
    )
    assert first.order is not None and second.order is not None
    assert first.order.client_order_id != second.order.client_order_id
    assert len(first.order.client_order_id) == 32


# --- Leverage and liquidation buffer (rules 5, 6) ---


@pytest.mark.parametrize(
    ("stop", "leverage"),
    [("199", 10), ("190", 6), ("180", 4), ("140", 1)],
)
def test_leverage_is_lowered_until_the_stop_sits_inside_the_buffer(stop, leverage):
    verdict = _evaluate(intent(stop=stop))
    assert verdict.approved
    assert verdict.leverage == leverage
    mmr = instrument().maintenance_margin_rate
    liquidation = liquidation_price(Direction.LONG, D(200), D(leverage), mmr)
    assert D(200) - D(stop) <= D("0.5") * (D(200) - liquidation)


def test_short_buffer_uses_the_short_liquidation_side():
    verdict = _evaluate(intent(Direction.SHORT, stop="220"))
    assert verdict.approved and verdict.leverage is not None
    mmr = instrument().maintenance_margin_rate
    liquidation = liquidation_price(Direction.SHORT, D(200), D(verdict.leverage), mmr)
    assert D(20) <= D("0.5") * (liquidation - D(200))


def test_stop_beyond_any_buffer_is_rejected():
    verdict = _evaluate(intent(stop="80"))
    assert verdict.decision is Decision.REJECTED
    assert _checks(verdict) == {RiskCheck.LIQUIDATION_BUFFER}


def test_leverage_respects_config_cap():
    risk = RISK.model_copy(update={"max_leverage": D("3.5")})
    assert _evaluate(risk=risk).leverage == 3


def test_leverage_respects_the_venue_risk_tier():
    big = state("10000000")
    risk = RISK.model_copy(update={"max_position_notional": D(10_000_000)})
    deep = snapshot(quantity="100000")
    verdict = _evaluate(account=big, risk=risk, snap=deep)
    # 70 000 pUSD risk over 1.00 → 70 000 units, far past the 1M tier (3x).
    assert verdict.leverage == 3


def test_liquidation_price_matches_the_simulator_model():
    from perps_agent.sim import SimPosition

    nvda = instrument()
    for direction, sign in ((Direction.LONG, 1), (Direction.SHORT, -1)):
        sim = SimPosition(
            instrument=nvda,
            quantity=D(2) * sign,
            entry_price=D(200),
            leverage=D(5),
            margin=D(2) * D(200) / D(5),
        )
        expected = liquidation_price(direction, D(200), D(5), nvda.maintenance_margin_rate)
        assert sim.liquidation_price() == expected


# --- Permissions and window (rule 1) ---


def test_outside_the_entry_window_is_rejected():
    after_cutoff = NOW.replace(hour=16, minute=20)  # 12:20 New York
    assert RiskCheck.ENTRY_WINDOW in _checks(_evaluate(at=after_cutoff))
    saturday = NOW + timedelta(days=4)
    assert RiskCheck.ENTRY_WINDOW in _checks(_evaluate(at=saturday))


def test_disabled_symbol_and_autonomy_level_are_rejected():
    off = (SymbolConfig(symbol="NVDA-USD", enabled=False),)
    assert _checks(_evaluate(symbols=off)) == {RiskCheck.SYMBOL_DISABLED}
    no_l2 = (SymbolConfig(symbol="NVDA-USD", autonomy=AutonomyConfig(L2=False)),)
    verdict = _evaluate(intent(level=AutonomyLevel.L2), symbols=no_l2)
    assert _checks(verdict) == {RiskCheck.AUTONOMY_LEVEL}
    assert _evaluate(symbols=no_l2).approved


def test_close_only_instrument_is_rejected():
    verdict = _evaluate(instr=instrument(close_only=True))
    assert _checks(verdict) == {RiskCheck.INSTRUMENT_CLOSED}


# --- Market quality (rule 2) ---


def test_stale_or_old_snapshots_are_rejected():
    stale = _evaluate(snap=snapshot(stale=True))
    assert _checks(stale) == {RiskCheck.STALE_MARKET}
    assert "book" in stale.reasons[0].detail
    old = _evaluate(snap=snapshot(taken_at=NOW - timedelta(seconds=3)))
    assert _checks(old) == {RiskCheck.STALE_MARKET}


def test_wide_spread_and_thin_book_are_rejected():
    wide = _evaluate(snap=snapshot(bid="199.90", ask="200.10"))
    assert _checks(wide) == {RiskCheck.SPREAD}
    thin = _evaluate(snap=snapshot(quantity="5"))  # 5 × 200 = 1000 < 2000
    assert _checks(thin) == {RiskCheck.DEPTH}


def test_one_sided_book_is_rejected():
    snap = snapshot().model_copy(update={"asks": ()})
    assert RiskCheck.NO_MARKET in _checks(_evaluate(snap=snap))


# --- Stop, target, time stop (rule 3) ---


def test_stop_on_the_wrong_side_is_rejected():
    assert _checks(_evaluate(intent(stop="201"))) == {RiskCheck.STOP_SIDE}
    assert _checks(_evaluate(intent(Direction.SHORT, stop="199"))) == {RiskCheck.STOP_SIDE}


def test_stop_inside_the_noise_floor_is_rejected():
    verdict = _evaluate(intent(stop="199.70"))  # 15 bp < 20 bp
    assert _checks(verdict) == {RiskCheck.STOP_DISTANCE}


def test_stop_is_rounded_away_from_the_entry():
    verdict = _evaluate(intent(stop="199.005"))
    assert verdict.stop_price == D("199.00")
    short = _evaluate(intent(Direction.SHORT, stop="200.995"))
    assert short.stop_price == D("201.00")


def test_target_on_the_wrong_side_is_rejected():
    verdict = _evaluate(intent(target_price=D(199)))
    assert _checks(verdict) == {RiskCheck.TARGET_SIDE}
    assert _evaluate(intent(target_price=D(203))).target_price == D(203)


def test_time_stop_must_be_inside_the_window():
    late = _evaluate(intent(time_stop=NOW.replace(hour=17)))  # 13:00 New York
    assert _checks(late) == {RiskCheck.TIME_STOP}
    past = _evaluate(intent(time_stop=NOW - timedelta(minutes=1)))
    assert _checks(past) == {RiskCheck.TIME_STOP}
    assert _evaluate(intent(time_stop=NOW + timedelta(minutes=30))).approved


def test_every_failure_is_reported_together():
    verdict = _evaluate(
        intent(stop="201", target_price=D(199)),
        snap=snapshot(bid="199.90", ask="200.10"),
        account=state(trades_today=12),
    )
    assert _checks(verdict) == {
        RiskCheck.STOP_SIDE,
        RiskCheck.TARGET_SIDE,
        RiskCheck.SPREAD,
        RiskCheck.MAX_TRADES,
    }


def test_venue_rules_are_checked():
    verdict = _evaluate(intent(entry="225", stop="224"))  # outside mark ± 10%
    assert _checks(verdict) == {RiskCheck.VENUE_RULE}
    assert "price_outside_band" in verdict.reasons[0].detail


# --- Portfolio limits (rules 7, 8) ---


def test_one_position_per_symbol():
    held = _evaluate(account=state(positions=(position(),)))
    assert _checks(held) == {RiskCheck.POSITION_EXISTS}
    working = _evaluate(account=state(pending_entries=frozenset({"NVDA-USD"})))
    assert _checks(working) == {RiskCheck.POSITION_EXISTS}


def test_max_concurrent_positions_counts_working_entries():
    busy = state(pending_entries=frozenset({"TSLA-USD", "MSFT-USD", "HOOD-USD"}))
    assert _checks(_evaluate(account=busy)) == {RiskCheck.MAX_POSITIONS}


def test_daily_trade_counts():
    assert _checks(_evaluate(account=state(trades_today=12))) == {RiskCheck.MAX_TRADES}
    l2 = intent(level=AutonomyLevel.L2)
    assert _checks(_evaluate(l2, account=state(l2_trades_today=4))) == {RiskCheck.MAX_L2_TRADES}
    assert _evaluate(account=state(l2_trades_today=4)).approved


def test_daily_loss_limit_halts_entries():
    risk_engine = engine()
    at_limit = state("485", day_start="500")  # −15 = −3%
    assert risk_engine.entries_halted(at_limit)
    assert not risk_engine.entries_halted(state("486", day_start="500"))
    assert _checks(_evaluate(account=at_limit)) == {RiskCheck.DAILY_LOSS}


def _tsla(quantity: str = "10", pnl: str = "0") -> PositionRisk:
    """An open TSLA long 400 → stop 399: 1.00 + 399 × 9 bp exit cost per unit."""
    return PositionRisk(
        symbol="TSLA-USD",
        direction=Direction.LONG,
        quantity=D(quantity),
        average_entry=D(400),
        stop_price=D(399),
        approved_risk=D("3.5"),
        unrealized_pnl=D(pnl),
    )


def test_budget_is_cut_to_the_room_left_under_the_daily_loss_limit():
    # −10 of the −15 limit: 5 left, more than the 3.43 budget.
    assert _evaluate(account=state("490", day_start="500")).decision is Decision.APPROVED
    # −12.5: 2.5 left, so the 3.4125 budget is cut to it.
    verdict = _evaluate(account=state("487.5", day_start="500"))
    assert verdict.decision is Decision.RESIZED
    assert _checks(verdict) == {RiskCheck.DAILY_LOSS}
    assert verdict.order is not None and verdict.order.quantity == D("1.9855")  # 2.5 / 1.2591
    assert verdict.risk_amount is not None and verdict.risk_amount <= D("2.5")


def test_open_positions_use_up_the_daily_loss_room():
    # 10 TSLA risk 10 + 3.591 exit costs: 1.409 of the 15 is left.
    verdict = _evaluate(account=state(positions=(_tsla(),)))
    assert verdict.decision is Decision.RESIZED
    assert _checks(verdict) == {RiskCheck.DAILY_LOSS}
    assert verdict.order is not None and verdict.order.quantity == D("1.1190")
    # 12 TSLA would lose 16.31 at its stop: no room at all.
    rejected = _evaluate(account=state(positions=(_tsla("12"),)))
    assert rejected.decision is Decision.REJECTED
    assert _checks(rejected) == {RiskCheck.DAILY_LOSS}


def test_an_open_loss_is_not_counted_twice():
    # TSLA down 5: the day is at −5 and the stop is 5 closer, so the room is unchanged.
    risk_engine = engine()
    flat = state(positions=(_tsla(),))
    down = state("495", day_start="500", positions=(_tsla(pnl="-5"),))
    assert risk_engine.daily_loss_room(flat) == risk_engine.daily_loss_room(down) == D("1.409")


def test_working_entries_use_up_the_daily_loss_room():
    verdict = _evaluate(account=state(pending_risk=D(14)))
    assert _checks(verdict) == {RiskCheck.DAILY_LOSS}
    assert verdict.order is not None and verdict.order.quantity == D("0.7942")  # 1 / 1.2591


def test_a_post_only_entry_is_sized_at_the_maker_fee():
    # 1.00 + 200 × 1.25 bp + 199 × 9 bp = 1.2041 per unit.
    verdict = _evaluate(intent(entry="200", post_only=True))
    assert verdict.order is not None and verdict.order.quantity == D("2.9067")


def test_loss_per_unit_counts_entry_and_exit_costs():
    assert loss_per_unit(D(200), D(199)) == D(1)
    assert loss_per_unit(
        D(200), D(201), entry_fee_rate=D("0.0004"), exit_cost_rate=D("0.0009")
    ) == D("1.2609")


# --- Adding to a position (rule 10) ---


def _add(trade=None, *, held=None, account=None):
    held = held or position(stop="199", approved_risk="7")
    return engine().evaluate_add(
        trade or intent(entry="200", stop="199"),
        instrument=instrument(),
        snapshot=snapshot(),
        state=account or state(positions=(held,)),
        now=NOW,
    )


def test_add_gets_its_budget_while_the_position_has_room():
    # Held: 2 @ 198 with the stop at 199, so it already locks profit in.
    verdict = _add()
    assert verdict.decision is Decision.APPROVED
    assert verdict.adding
    assert verdict.order is not None and verdict.order.quantity == D("2.7797")


def test_add_budget_is_cut_to_the_room_left():
    # Held: 2 @ 198, stop 197.5 → 1 pUSD + 0.3555 exit costs at risk of its 3
    # approved: 1.6445 left.
    held = position(entry="198", stop="197.5", approved_risk="3")
    verdict = _add(intent(entry="200", stop="197.5"), held=held)
    assert verdict.decision is Decision.RESIZED
    assert _checks(verdict) == {RiskCheck.ADD_RISK}
    # 1.6445 pUSD over 2.50 + 0.08 + 0.17775 per unit → 0.5963 units.
    assert verdict.order is not None and verdict.order.quantity == D("0.5963")


def test_add_with_no_room_is_rejected():
    held = position(entry="198", stop="196", approved_risk="4")
    verdict = _add(intent(entry="200", stop="196"), held=held)
    assert _checks(verdict) == {RiskCheck.ADD_RISK}


def test_add_needs_a_profitable_position_in_the_same_direction():
    assert _checks(_add(held=position(stop="199", pnl="0"))) == {RiskCheck.NOT_IN_PROFIT}
    short = position(Direction.SHORT, entry="202", stop="203")
    assert RiskCheck.NO_POSITION in _checks(_add(held=short))
    assert _checks(_add(account=state())) == {RiskCheck.NO_POSITION}


def test_add_cannot_loosen_the_stop():
    verdict = _add(intent(entry="200", stop="199"), held=position(stop="199.2"))
    assert _checks(verdict) == {RiskCheck.STOP_WIDENED}


def test_add_counts_the_whole_position_against_the_notional_cap():
    held = position(quantity="24", entry="198", stop="199.5", approved_risk="100")
    verdict = _add(intent(entry="200", stop="199.5"), account=state("100000", positions=(held,)))
    assert RiskCheck.NOTIONAL_CAP in _checks(verdict)
    # 5000 − 24 × 200 = 200 pUSD left → 1 unit.
    assert verdict.order is not None and verdict.order.quantity == D(1)


# --- Stop changes (rule 11) ---


def test_stops_only_tighten():
    risk_engine = engine()
    held = position(stop="197")
    assert risk_engine.check_stop_change(held, D("197.5"), mark=D(200)) is None
    widened = risk_engine.check_stop_change(held, D("196.5"), mark=D(200))
    assert widened is not None and widened.check is RiskCheck.STOP_WIDENED
    through = risk_engine.check_stop_change(held, D(200), mark=D(200))
    assert through is not None and through.check is RiskCheck.STOP_SIDE
    short = position(Direction.SHORT, entry="202", stop="203")
    assert risk_engine.check_stop_change(short, D("202.5"), mark=D(200)) is None
    assert risk_engine.check_stop_change(short, D("203.5"), mark=D(200)) is not None


# --- Construction and intent validation ---


def test_engine_refuses_unset_risk_values():
    with pytest.raises(ValueError, match="max_leverage"):
        engine(RiskConfig())


def test_intent_validation():
    with pytest.raises(ValidationError, match="playbook"):
        intent(level=AutonomyLevel.L1, playbook=None)
    with pytest.raises(ValidationError, match="not tied to a playbook"):
        intent(level=AutonomyLevel.L2, playbook="orb@1")
    with pytest.raises(ValidationError, match="post_only"):
        intent(entry=None, post_only=True)
    with pytest.raises(ValidationError):
        intent(size="1.5")
    with pytest.raises(ValidationError):
        intent(size="0")


def test_post_only_limit_is_passed_through():
    verdict = _evaluate(intent(post_only=True, entry="199.99"))
    assert verdict.order is not None and verdict.order.post_only


def test_limit_entry_price_rounds_passively():
    verdict = _evaluate(intent(entry="199.999", stop="199"))
    assert verdict.order is not None and verdict.order.price == D("199.99")
    short = _evaluate(intent(Direction.SHORT, entry="200.001", stop="201"))
    assert short.order is not None and short.order.price == D("200.01")


def test_verdict_round_trips_as_json():
    verdict = _evaluate()
    assert RiskVerdict.model_validate_json(verdict.model_dump_json()) == verdict
