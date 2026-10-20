from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from perps_agent.risk import AutonomyLevel, Direction, TradeIntent
from perps_agent.strategy import Regime, StructureRegime, VolatilityRegime, VolumeProfile
from perps_agent.strategy.playbooks import (
    MAX_HOLD_MINUTES,
    PLAYBOOK_TYPES,
    Conviction,
    LiquiditySweep,
    MeanReversion,
    MomentumScalp,
    MomentumScalpParams,
    OpeningFlush,
    PlaybookParamsError,
    TrendFollowing,
    VwapReversion,
    evaluate_all,
    load_playbooks,
    parameter_bounds,
)
from tests.unit.risk_factories import engine, snapshot, state
from tests.unit.strategy_factories import (
    CONTEXT,
    OPEN,
    WINDOW,
    features,
    flat,
    history,
    minutes,
    path,
)
from tests.unit.venue_factories import instrument

D = Decimal
TREND_UP = Regime(VolatilityRegime.NORMAL, StructureRegime.TREND_UP, D(1))
COMPRESSED = Regime(VolatilityRegime.COMPRESSED, StructureRegime.ROTATION, D("0.5"))


def _r(setup):
    assert setup.reward_risk is not None
    return setup.reward_risk.quantize(D("0.0001"))


def _assert_stop_clears_the_floor(snap, setup):
    floor = CONTEXT.stop_floor(setup.reference_price, snap.volatility.range_15m)
    assert abs(setup.reference_price - setup.stop_price) >= floor


# --- VWAP reversion ---


def _stretched_then_turned():
    base = flat(OPEN, 30, "200", volume="30")
    rise = path(OPEN + minutes(30), [D(200) + D("0.4") * i for i in range(1, 11)], first_open="200")
    top = flat(OPEN + minutes(40), 15, "204")
    turn = path(OPEN + minutes(55), ["203.9"], first_open="204")
    return base + rise + top + turn


def test_vwap_reversion_fades_the_stretch_towards_vwap():
    snap = features(_stretched_then_turned())
    setup = VwapReversion().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert setup.entry_price == D("203.9")
    assert setup.target_price == snap.session.vwap
    assert setup.reward_risk is not None and setup.reward_risk >= D("2.2")
    _assert_stop_clears_the_floor(snap, setup)
    assert setup.time_stop == snap.at + minutes(60)
    assert setup.playbook.startswith("vwap_reversion@1.")


def test_vwap_reversion_waits_for_the_turn():
    bars = _stretched_then_turned()[:-1]
    assert VwapReversion().evaluate(features(bars), CONTEXT) is None


# --- Momentum scalp ---


def _breakout(volume: str = "30"):
    rise = path(OPEN, [D(200) + D("0.1") * i for i in range(1, 31)], first_open="200")
    last = path(OPEN + minutes(30), ["203.5"], first_open="203", volume=volume)
    return rise + last


def test_momentum_scalp_joins_a_volume_breakout_in_a_trend():
    snap = replace(features(_breakout()), regime=TREND_UP)
    setup = MomentumScalp().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert setup.entry_price is None  # marketable
    assert _r(setup) == D("2.5")
    assert setup.time_stop == snap.at + minutes(10)
    _assert_stop_clears_the_floor(snap, setup)


def test_momentum_scalp_needs_volume_and_a_trend():
    assert (
        MomentumScalp().evaluate(replace(features(_breakout("10")), regime=TREND_UP), CONTEXT)
        is None
    )
    assert (
        MomentumScalp().evaluate(replace(features(_breakout()), regime=COMPRESSED), CONTEXT) is None
    )
    compressed_trend = Regime(VolatilityRegime.COMPRESSED, StructureRegime.TREND_UP, D("0.5"))
    assert (
        MomentumScalp().evaluate(replace(features(_breakout()), regime=compressed_trend), CONTEXT)
        is None
    )


def test_scalp_hold_never_exceeds_15_minutes():
    assert MAX_HOLD_MINUTES[MomentumScalp.kind] == 15
    with pytest.raises(PlaybookParamsError):
        load_playbooks({"momentum_scalp": {"max_hold_minutes": 16}})


# --- Mean reversion ---


def _value_area_poke():
    base = flat(OPEN, 40, "200", wick="0.02")
    poke = path(OPEN + minutes(40), ["200.25", "200.1"], first_open="200", wick="0.02")
    return base + poke


def test_mean_reversion_fades_a_value_area_poke_in_a_compressed_session():
    snap = features(_value_area_poke())
    profile = VolumeProfile(
        poc=D("198.2"),
        value_area_low=D("198"),
        value_area_high=D("200.2"),
        bucket_size=D("0.1"),
        volume=D(400),
    )
    snap = replace(snap, regime=COMPRESSED, profile=profile)
    setup = MeanReversion().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert setup.variant == "value_area"
    assert setup.conviction is Conviction.NORMAL and setup.max_size == 1
    assert setup.target_price == D("198.2")
    _assert_stop_clears_the_floor(snap, setup)
    # Not compressed: the value-area variant stays quiet.
    assert MeanReversion().evaluate(replace(snap, regime=TREND_UP), CONTEXT) is None


def test_mean_reversion_fades_a_failed_breakout_against_the_trend_with_caution():
    base = flat(OPEN, 40, "200", wick="0.02")
    failed = path(OPEN + minutes(40), ["200.25", "200.1"], first_open="200", wick="0.02")
    # Five sessions ago it closed at 205: a falling ticker.
    snap = features(base + failed, prior=history(10, close="205"))
    assert snap.session is not None and snap.session.opening_range_high == D("200.02")
    profile = VolumeProfile(
        poc=D("198.2"),
        value_area_low=D("198"),
        value_area_high=D("200.5"),
        bucket_size=D("0.1"),
        volume=D(400),
    )
    setup = MeanReversion().evaluate(replace(snap, profile=profile, regime=TREND_UP), CONTEXT)
    assert setup is None  # closed at 200.1, still above the range high
    inside = path(OPEN + minutes(40), ["200.25", "200.0"], first_open="200", wick="0.02")
    snap = replace(features(base + inside, prior=history(10, close="205")), profile=profile)
    setup = MeanReversion().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert setup.variant == "breakout_fade"
    assert setup.conviction is Conviction.CAUTION
    assert setup.max_size == D("0.5")


# --- Liquidity sweep ---


def _sweep(sweep_volume: str = "40", reclaim: str = "199.6"):
    base = path(OPEN, ["199.6", "199.8", "200"] * 7, first_open="199.7")
    sweep = path(
        OPEN + minutes(21), ["199.5", "198.9"], first_open="199.7", volumes=["10", sweep_volume]
    )
    back = path(OPEN + minutes(23), [reclaim], first_open="198.9")
    return base + sweep + back


def test_liquidity_sweep_buys_the_reclaim_of_a_swept_low():
    snap = features(_sweep(), prior=history(10, low="199.2"))
    setup = LiquiditySweep().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert setup.conviction is Conviction.HIGH
    assert _r(setup) == D("2.5")
    _assert_stop_clears_the_floor(snap, setup)


def test_liquidity_sweep_conviction_and_reclaim():
    normal = LiquiditySweep().evaluate(
        features(_sweep("12"), prior=history(10, low="199.2")), CONTEXT
    )
    assert normal is not None and normal.conviction is Conviction.NORMAL
    no_reclaim = features(_sweep(reclaim="199.0"), prior=history(10, low="199.2"))
    assert LiquiditySweep().evaluate(no_reclaim, CONTEXT) is None


# --- Trend following ---


def test_trend_following_buys_a_pullback_to_vwap_in_a_calm_trend():
    bars = path(OPEN, [D(200) + D("0.02") * i for i in range(1, 61)], first_open="200")
    snap = features(bars)
    assert snap.session is not None
    session = replace(
        snap.session, vwap=snap.price - D("0.1"), efficiency=D("0.7"), share_above_vwap=D("0.9")
    )
    snap = replace(snap, session=session, regime=TREND_UP)
    setup = TrendFollowing().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert setup.target_price is None  # a runner
    _assert_stop_clears_the_floor(snap, setup)
    # Overextended from VWAP, or in an expanding session: quiet.
    far = replace(snap, session=replace(session, vwap=snap.price - D(2)))
    assert TrendFollowing().evaluate(far, CONTEXT) is None
    hot = Regime(VolatilityRegime.EXPANSION, StructureRegime.TREND_UP, D(2))
    assert TrendFollowing().evaluate(replace(snap, regime=hot), CONTEXT) is None


# --- Opening flush (swing) ---


def _flush():
    down = path(OPEN, [D(205) - D("0.3") * i for i in range(1, 11)], first_open="205")
    up = path(OPEN + minutes(10), [D(202) + D("0.3") * i for i in range(1, 6)], first_open="202")
    return down + up


def test_opening_flush_buys_a_beaten_down_reclaim():
    snap = features(_flush(), prior=history(10, close="215"))
    setup = OpeningFlush().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.direction is Direction.LONG
    assert _r(setup) == D(3)
    _assert_stop_clears_the_floor(snap, setup)


def test_opening_flush_needs_a_beaten_down_ticker():
    assert (
        OpeningFlush().evaluate(features(_flush(), prior=history(10, close="205")), CONTEXT) is None
    )


def test_opening_flush_is_long_only():
    assert OpeningFlush.long_only
    rally = path(OPEN, [D(195) + D("0.3") * i for i in range(1, 16)], first_open="195")
    assert OpeningFlush().evaluate(features(rally, prior=history(10, close="185")), CONTEXT) is None


# --- Common behaviour ---


def test_playbooks_are_quiet_outside_the_entry_window():
    bars = _breakout()
    late = [replace(b, start=b.start + (WINDOW.entry_cutoff - OPEN)) for b in bars]
    snap = replace(features(late, at=WINDOW.entry_cutoff + minutes(1)), regime=TREND_UP)
    assert evaluate_all(load_playbooks(), snap, CONTEXT) == ()


def test_time_stop_is_capped_at_the_window_end():
    bars = [
        replace(b, start=b.start + (WINDOW.entry_cutoff - OPEN) - minutes(60))
        for b in _stretched_then_turned()
    ]
    snap = features(bars)
    setup = VwapReversion().evaluate(snap, CONTEXT)
    assert setup is not None
    assert setup.time_stop == WINDOW.end


def test_setup_turns_into_an_intent_the_risk_engine_approves():
    snap = features(_sweep(), prior=history(10, low="199.2"))
    setup = LiquiditySweep().evaluate(snap, CONTEXT)
    assert setup is not None
    intent = TradeIntent(
        symbol="NVDA-USD",
        direction=setup.direction,
        level=AutonomyLevel.L1,
        playbook=setup.playbook,
        entry_price=setup.entry_price,
        size=D(1),
        stop_price=setup.stop_price,
        target_price=setup.target_price,
        time_stop=setup.time_stop,
        thesis="; ".join(setup.reasons),
        confidence=D("0.6"),
    )
    verdict = engine().evaluate(
        intent,
        instrument=instrument(),
        snapshot=snapshot(bid="199.58", ask="199.62", mark="199.6", taken_at=snap.at),
        state=state(),
        range_15m=snap.volatility.range_15m,
        now=snap.at,
    )
    assert verdict.approved, verdict.reasons


# --- Parameters, bounds and versions ---


def test_every_parameter_has_bounds_and_a_default_inside_them():
    for kind in PLAYBOOK_TYPES:
        bounds = parameter_bounds(kind.params_type)
        assert bounds, kind.id
        for b in bounds:
            assert b.minimum <= b.default <= b.maximum, (kind.id, b.name)
        hold = next(b for b in bounds if b.name == "max_hold_minutes")
        assert hold.maximum <= MAX_HOLD_MINUTES[kind.kind]


def test_parameters_outside_the_bounds_are_refused():
    with pytest.raises(PlaybookParamsError, match="vwap_reversion"):
        load_playbooks({"vwap_reversion": {"min_target_r": "1.5"}})
    with pytest.raises(PlaybookParamsError, match="unknown"):
        load_playbooks({"orb": {}})
    with pytest.raises(PlaybookParamsError):
        load_playbooks({"trend_following": {"no_such_parameter": 1}})


def test_a_parameter_change_is_a_new_version():
    default = MomentumScalp()
    tuned = MomentumScalp(MomentumScalpParams(volume_multiple=D(2)))
    assert default.tag != tuned.tag
    assert default.tag == MomentumScalp().tag
    assert tuned.tag.startswith("momentum_scalp@1.")
    assert [p.id for p in load_playbooks()] == [
        "vwap_reversion",
        "momentum_scalp",
        "mean_reversion",
        "liquidity_sweep",
        "trend_following",
        "opening_flush",
    ]


def test_no_playbook_fires_without_a_15_minute_range():
    snap = features(_sweep(), prior=history(10, low="199.2"))
    blind = replace(snap, volatility=replace(snap.volatility, range_15m=None))
    assert evaluate_all(load_playbooks(), blind, CONTEXT) == ()


def test_holding_time_starts_from_the_snapshot():
    snap = features(_flush(), prior=history(10, close="215"))
    setup = OpeningFlush().evaluate(snap, CONTEXT)
    assert setup is not None and setup.time_stop - snap.at == timedelta(minutes=120)


def test_playbooks_doc_lists_the_coded_bounds():
    from tests.conftest import REPO_ROOT

    text = (REPO_ROOT / "docs" / "architecture" / "playbooks.md").read_text()
    for kind in PLAYBOOK_TYPES:
        assert f"### `{kind.id}`" in text
        for b in parameter_bounds(kind.params_type):
            assert f"| `{b.name}` | {b.default} | {b.minimum} | {b.maximum} |" in text, (kind.id, b)
