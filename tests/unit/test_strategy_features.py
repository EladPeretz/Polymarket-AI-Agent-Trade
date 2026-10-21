from datetime import timedelta
from decimal import Decimal

import pytest

from perps_agent.strategy import (
    MinuteBars,
    StructureRegime,
    TickerCharacter,
    VolatilityRegime,
    bar_from_candle,
    minute_volatility,
    summarize_session,
    ticker_profile,
    volume_profile,
)
from perps_agent.venue import Candle, Side, Trade
from tests.unit.strategy_factories import (
    FEATURES,
    OPEN,
    WINDOW,
    bar,
    features,
    flat,
    history,
    minutes,
    path,
    summary,
)

D = Decimal


def _trade(trade_id: int, seconds: float, price: str, quantity: str = "1", **kw: object) -> Trade:
    return Trade(
        trade_id=trade_id,
        instrument_id=19,
        aggressor=Side.BUY,
        price=D(price),
        quantity=D(quantity),
        timestamp=OPEN + timedelta(seconds=seconds),
        **kw,  # type: ignore[arg-type]
    )


# --- Bars ---


def test_trades_build_minute_bars_with_exact_notional():
    bars = MinuteBars()
    bars.add_trades(
        [
            _trade(1, 1, "200"),
            _trade(2, 20, "201", "2"),
            _trade(3, 59, "199.5"),
            _trade(4, 61, "198"),
        ]
    )
    first, second = bars.bars()
    assert (first.open, first.high, first.low, first.close) == (
        D(200),
        D(201),
        D("199.5"),
        D("199.5"),
    )
    assert first.volume == D(4)
    assert first.notional == D(200) + D(402) + D("199.5")
    assert first.average_price == first.notional / 4
    assert second.start == OPEN + minutes(1)


def test_duplicate_and_settlement_trades_are_ignored():
    bars = MinuteBars()
    bars.add_trades(
        [_trade(1, 1, "200"), _trade(1, 1, "200"), _trade(2, 2, "300", settlement=True)]
    )
    (only,) = bars.bars()
    assert only.volume == 1 and only.high == 200


def test_marks_fill_minutes_without_trades():
    bars = MinuteBars()
    bars.add_trades([_trade(1, 1, "200")])
    bars.add_mark(D("200.5"), OPEN + timedelta(seconds=30))  # traded minute: untouched
    bars.add_mark(D(201), OPEN + timedelta(seconds=65))
    bars.add_mark(D("201.4"), OPEN + timedelta(seconds=80))
    bars.add_mark(D("200.8"), OPEN + timedelta(seconds=110))
    traded, marked = bars.bars()
    assert traded.high == 200 and traded.volume == 1
    assert (marked.open, marked.high, marked.low, marked.close) == (
        D(201),
        D("201.4"),
        D("200.8"),
        D("200.8"),
    )
    assert marked.volume == 0
    # A trade in a mark-filled minute makes it a traded bar.
    bars.add_trades([_trade(2, 115, "200.9")])
    assert bars.bars()[1].volume == 1 and bars.bars()[1].high == D("201.4")


def test_sparse_volume_switches_vwap_and_profile_to_time_weighting():
    # One heavy trade at 200 at the open, then 9 price-only minutes at 205.
    session = path(OPEN, ["200"], wick="0", volume="10") + path(
        OPEN + minutes(1), ["205"] * 9, first_open="205", wick="0", volume="0"
    )
    snap = features(session, price="205")
    s = snap.session
    assert s is not None and s.volume_sparse
    assert s.vwap == (D(200) + 9 * D(205)) / 10
    assert snap.profile is not None and snap.profile.value_area_low > 200
    # With volume on most minutes, VWAP stays volume-weighted.
    traded = path(OPEN, ["200", "205"], wick="0", volumes=["30", "10"])
    dense = features(traded, price="205").session
    assert dense is not None and not dense.volume_sparse
    assert dense.vwap == sum(b.notional for b in traded) / 40


def test_candles_seed_bars_and_trades_merge_into_them():
    candle = Candle(
        instrument_id=19,
        interval="1m",
        open_time=OPEN,
        open=D(200),
        high=D(201),
        low=D(199),
        close=D(200),
        volume=D(3),
        trade_count=3,
    )
    bars = MinuteBars(keep=timedelta(minutes=30))
    bars.seed([candle])
    assert bars.bars()[0].notional == D(200) * 3
    bars.add_trades([_trade(9, 30, "202")])
    merged = bars.bars()[0]
    assert merged.high == 202 and merged.close == 202 and merged.volume == 4
    bars.prune(OPEN + minutes(31))
    assert bars.bars() == []
    with pytest.raises(ValueError):
        bar_from_candle(candle.model_copy(update={"interval": "5m"}))


# --- Volatility ---


def test_ranges_cover_the_last_15_and_60_minutes_and_the_price():
    early = path(OPEN - minutes(60), [200] * 45, wick="0")  # 12:30–13:15 UTC, flat at 200
    late = path(
        OPEN - minutes(15), ["200.5", "201", "201.5"] + ["201"] * 12, first_open="200", wick="0"
    )
    snap = features(early + late, at=OPEN, price="201.2")
    vol = snap.volatility
    assert vol.range_15m == D("1.5")  # 200 (first open) to 201.5
    assert vol.range_60m == D("1.5")
    assert vol.range_15m_bp == D("1.5") / D("201.2") * 10_000
    # Price outside the bars widens the range.
    assert features(late, at=OPEN, price="203").volatility.range_15m == D(3)


def test_expected_move_scales_minute_volatility_to_the_window_end():
    closes = [D(200) * (D("1.001") if i % 2 else 1) for i in range(61)]
    bars = path(OPEN, closes, wick="0")
    snap = features(bars)
    vol = snap.volatility
    sigma = minute_volatility(bars[-60:])
    assert sigma is not None and vol.minute_vol_bp == sigma * 10_000
    assert vol.minutes_to_close == 180 - 61
    assert vol.expected_move_to_close == snap.price * sigma * D(119).sqrt()
    # Before the window the whole window is left; afterwards nothing.
    assert (
        features(flat(OPEN - minutes(30), 30), at=OPEN - minutes(1)).volatility.minutes_to_close
        == 180
    )
    assert features(bars, at=WINDOW.end + minutes(1)).volatility.minutes_to_close == 0


def test_volatility_is_unknown_without_bars():
    snap = features(flat(OPEN - minutes(90), 5), at=OPEN)
    assert snap.volatility.range_15m is None
    assert snap.volatility.minute_vol_bp is None
    assert snap.volatility.expected_move_to_close is None


# --- Volume profile ---


def test_volume_profile_finds_poc_and_value_area():
    bars = [
        bar(OPEN, 200, 200, 200, 200, "10"),
        bar(OPEN + minutes(1), 201, 201, 201, 201, "50"),
        bar(OPEN + minutes(2), 202, 202, 202, 202, "30"),
        bar(OPEN + minutes(3), 205, 205, 205, 205, "10"),
    ]
    profile = volume_profile(bars, D(1), D("0.7"))
    assert profile is not None
    assert profile.poc == D("201.5")  # middle of the [201, 202) bucket
    # 50 at 201, then the heavier neighbour 202 (30): 80 of 100 ≥ 70%.
    assert (profile.value_area_low, profile.value_area_high) == (D(201), D(203))
    assert volume_profile([bar(OPEN, 1, 1, 1, 1, "0")], D(1), D("0.7")) is None


def test_volume_profile_crosses_empty_buckets():
    bars = [bar(OPEN, 200, 200, 200, 200, "50"), bar(OPEN + minutes(1), 204, 204, 204, 204, "50")]
    profile = volume_profile(bars, D(1), D("0.7"))
    assert profile is not None
    assert profile.poc == D("200.5")
    assert (profile.value_area_low, profile.value_area_high) == (D(200), D(205))


# --- Session ---


def test_session_features_from_the_open():
    pre = flat(OPEN - minutes(30), 30, "199")
    session = path(OPEN, ["200", "201", "202", "201", "203"], first_open="200", wick="0")
    snap = features(pre + session, price="203")
    s = snap.session
    assert s is not None
    assert s.started_at == OPEN and s.minutes_elapsed == 5
    assert s.open == D(200) and s.high == D(203) and s.low == D(200)
    assert s.price_vs_open_bp == D(150)
    assert s.efficiency == 1
    assert s.vwap == sum(b.notional for b in session) / sum(b.volume for b in session)
    assert not s.opening_range_complete
    assert (s.opening_range_high, s.opening_range_low) == (D(203), D(200))
    # Prior sessions average 10 per minute; this one has traded 50 in 5 minutes.
    assert s.relative_volume == 1


def test_vwap_crosses_and_share_above():
    session = path(OPEN, ["201", "199", "201", "199", "201"], first_open="200", wick="0")
    s = features(session).session
    assert s is not None
    assert s.vwap_crosses == 4
    assert s.share_above_vwap == D(3) / 5


def test_no_session_before_the_open():
    snap = features(flat(OPEN - minutes(30), 30), at=OPEN - minutes(1))
    assert snap.session is None
    assert snap.profile is None
    assert snap.regime.labels == ()


def test_summarize_session_and_ticker_profile():
    market = WINDOW.market
    bars = path(market.open, ["201", "202", "204"], first_open="200", wick="0")
    s = summarize_session(bars, market, FEATURES)
    assert s is not None
    assert (s.open, s.high, s.low, s.close) == (D(200), D(204), D(200), D(204))
    assert s.efficiency == 1 and s.return_bp == D(200)

    trending = ticker_profile(
        history(6, open="200", high="210", low="200", close="210"), D(189), FEATURES
    )
    assert trending is not None
    assert trending.character is TickerCharacter.TRENDING
    assert trending.return_1d_bp == D(-1000)
    assert trending.return_5d_bp == D(-1000)
    assert trending.up_sessions == 6
    choppy = ticker_profile(history(3, close="200"), D(200), FEATURES)
    assert choppy is not None and choppy.character is TickerCharacter.CONSOLIDATING
    assert choppy.return_5d_bp is None
    assert ticker_profile(history(1), D(200), FEATURES) is None


def test_key_levels_include_the_prior_session_and_the_opening_range():
    prior = (
        summary(WINDOW.date - timedelta(days=1), high="205", low="195", close="201", poc="199"),
    )
    early = features(flat(OPEN, 5), prior=prior)
    assert {lv.name for lv in early.levels} == {
        "prior_high",
        "prior_low",
        "prior_close",
        "prior_poc",
    }
    later = features(flat(OPEN, 20), prior=prior)
    names = {lv.name: lv.price for lv in later.levels}
    assert names["opening_range_high"] == D("200.05") and names["prior_poc"] == D(199)


# --- Regime ---


def test_trend_up_in_expansion():
    closes = [D(200) + D("0.4") * i for i in range(1, 31)]
    snap = features(path(OPEN, closes, first_open="200"))
    assert snap.regime.structure is StructureRegime.TREND_UP
    # 12 / 200 = 600 bp in 30 minutes vs 300 bp × √(30/390) usual: ratio ≈ 7.
    assert snap.regime.volatility is VolatilityRegime.EXPANSION
    assert snap.regime.labels == ("expansion", "trend_up")


def test_rotation_in_a_compressed_session():
    closes = ["200.1", "199.9"] * 15
    snap = features(path(OPEN, closes, first_open="200", wick="0"))
    assert snap.regime.structure is StructureRegime.ROTATION
    assert snap.regime.volatility is VolatilityRegime.COMPRESSED


def test_regime_is_unknown_early_and_volatility_unknown_without_history():
    assert features(flat(OPEN, 10)).regime.structure is None
    snap = features(flat(OPEN, 20), prior=())
    assert snap.regime.structure is StructureRegime.ROTATION
    assert snap.regime.volatility is None
