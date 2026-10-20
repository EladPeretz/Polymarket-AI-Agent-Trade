from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from perps_agent.config.models import TriggersConfig
from perps_agent.journal import TriggerKind
from perps_agent.risk import Direction
from perps_agent.strategy import TriggerDetector, WatchedPosition
from perps_agent.strategy.playbooks import Conviction, Setup
from tests.unit.strategy_factories import CALENDAR, OPEN, WINDOW, features, flat, minutes

D = Decimal


def _detector(**config: object) -> TriggerDetector:
    return TriggerDetector(CALENDAR, TriggersConfig(**config), check_in_minutes=15)  # type: ignore[arg-type]


def _setup(direction: Direction = Direction.LONG, playbook: str = "vwap_reversion@1.abc") -> Setup:
    return Setup(
        playbook=playbook,
        symbol="NVDA-USD",
        direction=direction,
        reference_price=D(200),
        entry_price=D(200),
        stop_price=D(199),
        target_price=None,
        time_stop=OPEN + minutes(60),
        conviction=Conviction.NORMAL,
        max_size=D(1),
        reward_risk=None,
        reasons=("stretched",),
    )


def _snap(at_minute: float = 30, price: str = "200"):
    bars = flat(OPEN, 30)
    return replace(features(bars), at=OPEN + minutes(at_minute), price=D(price))


def test_a_playbook_setup_fires_once_until_it_goes_away():
    detector = _detector()
    (first,) = detector.on_features(_snap(), [_setup()])
    assert first.kind is TriggerKind.PLAYBOOK
    assert first.key == "playbook:vwap_reversion:long"
    assert first.setup is not None
    assert detector.on_features(_snap(30.1), [_setup()]) == []
    # Another direction, or the same setup after it went away, fires again.
    assert len(detector.on_features(_snap(30.2), [_setup(), _setup(Direction.SHORT)])) == 1
    assert detector.on_features(_snap(30.3), []) == []
    assert len(detector.on_features(_snap(30.4), [_setup()])) == 1


def test_a_large_move_wakes_the_agent_once():
    detector = _detector()
    snap = replace(_snap(), volatility=replace(_snap().volatility, minute_vol_bp=D(2)))
    assert detector.on_features(snap, []) == []
    # 3σ over 60 s is 6 bp, so the 15 bp minimum applies: 10 bp is not enough.
    later = replace(snap, at=snap.at + timedelta(seconds=20), price=D("200.2"))
    assert detector.on_features(later, []) == []
    big = replace(snap, at=snap.at + timedelta(seconds=40), price=D("200.4"))
    (move,) = detector.on_features(big, [])
    assert move.kind is TriggerKind.LARGE_MOVE
    assert move.key == "large_move:up"
    after = replace(snap, at=snap.at + timedelta(seconds=45), price=D("200.45"))
    assert detector.on_features(after, []) == []


def test_large_move_threshold_scales_with_volatility():
    detector = _detector()
    snap = replace(_snap(), volatility=replace(_snap().volatility, minute_vol_bp=D(10)))
    detector.on_features(snap, [])
    # 3 × 10 bp = 30 bp needed; 20 bp is not a large move for this symbol now.
    later = replace(snap, at=snap.at + timedelta(seconds=30), price=D("199.6"))
    assert detector.on_features(later, []) == []
    # Moves older than the window are forgotten.
    old = replace(snap, at=snap.at + timedelta(seconds=200), price=D("199.0"))
    assert detector.on_features(old, []) == []


def test_position_events_fire_once_each():
    detector = _detector(large_move_min_bp=500)
    position = WatchedPosition(
        symbol="NVDA-USD",
        direction=Direction.LONG,
        entry_price=D(200),
        stop_price=D(199),
        target_price=D(203),
        time_stop=OPEN + minutes(40),
    )
    assert detector.on_features(_snap(price="200.5"), [], position) == []
    (near_stop,) = detector.on_features(_snap(31, "199.2"), [], position)
    assert near_stop.kind is TriggerKind.POSITION and near_stop.key == "position:stop"
    assert detector.on_features(_snap(32, "199.1"), [], position) == []
    (near_target,) = detector.on_features(_snap(33, "202.5"), [], position)
    assert near_target.key == "position:target"
    (time_stop,) = detector.on_features(_snap(38, "202"), [], position)
    assert time_stop.key == "position:time_stop"
    # A tighter stop is a new stop to watch.
    tighter = replace(position, stop_price=D("201.5"))
    (again,) = detector.on_features(_snap(39, "201.7"), [], tighter)
    assert again.key == "position:stop"
    # Flat: the memory clears.
    assert detector.on_features(_snap(40), [], None) == []
    keys = {t.key for t in detector.on_features(_snap(41, "199.2"), [], position)}
    assert keys == {"position:stop", "position:time_stop"}


def test_short_position_near_stop():
    detector = _detector()
    position = WatchedPosition("NVDA-USD", Direction.SHORT, D(200), D(201))
    (event,) = detector.on_features(_snap(price="200.8"), [], position)
    assert event.key == "position:stop"


def test_session_milestones_and_check_ins():
    detector = _detector()
    assert detector.on_clock(OPEN - minutes(1)) == []
    (opened,) = detector.on_clock(OPEN)
    assert opened.key == "session:open"
    assert detector.on_clock(OPEN + minutes(14)) == []
    (check_in,) = detector.on_clock(OPEN + minutes(15))
    assert check_in.kind is TriggerKind.CHECK_IN
    assert detector.on_clock(OPEN + minutes(20)) == []
    # Called late: one check-in, not one per missed interval.
    assert [t.kind for t in detector.on_clock(OPEN + minutes(61))] == [TriggerKind.CHECK_IN]
    keys = [t.key for t in detector.on_clock(WINDOW.entry_cutoff)]
    assert keys == ["session:entry_cutoff", "check_in"]
    (closed,) = detector.on_clock(WINDOW.end)
    assert closed.key == "session:close"
    assert detector.on_clock(WINDOW.end + minutes(15)) == []


def test_milestones_reset_on_the_next_day():
    detector = _detector()
    detector.on_clock(OPEN)
    (opened,) = detector.on_clock(OPEN + timedelta(days=1))
    assert opened.key == "session:open"
