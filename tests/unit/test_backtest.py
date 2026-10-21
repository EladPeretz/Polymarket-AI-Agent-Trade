"""The backtest runner's fill model, the Gate 1 screen and its records (WP13)."""

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from perps_agent.app.backtest import run_backtest, window_days
from perps_agent.backtest import (
    Backtester,
    BacktestResult,
    BacktestTrade,
    DayData,
    ExitReason,
    HistoryCache,
    PlaybookTally,
    SessionInfo,
    evaluate,
    mark_bars,
    read_day,
    read_records,
    screened_playbooks,
    write_day,
    write_records,
)
from perps_agent.config.models import (
    AppConfig,
    BacktestConfig,
    SimConfig,
    SymbolConfig,
)
from perps_agent.risk import Direction
from perps_agent.strategy import Bar, FeatureSnapshot
from perps_agent.strategy.playbooks import (
    ANY_REGIME,
    Conviction,
    Playbook,
    PlaybookContext,
    PlaybookKind,
    PlaybookParams,
    Setup,
    load_playbooks,
)
from perps_agent.strategy.regime import Regime, StructureRegime, VolatilityRegime
from perps_agent.venue import Candle, FundingRate, MarkPoint
from perps_agent.venue.fake import FakeExchange
from tests.unit.risk_factories import RISK
from tests.unit.strategy_factories import CALENDAR, WINDOW, bar, flat, minutes
from tests.unit.venue_factories import T0, instrument

D = Decimal
DAY = date(2026, 10, 20)
CONFIG = AppConfig(
    symbols=(SymbolConfig(symbol="NVDA-USD"),),
    risk=RISK,
    sim=SimConfig(starting_collateral=D(500)),
)
# 0.7% of 500 pUSD.
BUDGET = D("3.5")
TAKER, MAKER, SLIP = D("0.0004"), D("0.000125"), D("0.0005")
AT = T0 + minutes(30)  # 10:00 New York
assert WINDOW is not None


class _Params(PlaybookParams):
    pass


class Scripted(Playbook[_Params]):
    """Shows the scripted setups at the given minutes, as a real playbook would."""

    id = "scripted"
    logic_version = 1
    kind = PlaybookKind.INTRADAY
    regimes = (ANY_REGIME,)
    params_type = _Params
    description = "test double"
    script: ClassVar[dict[datetime, Setup]] = {}

    def evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        return self.script.get(features.at)

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        raise NotImplementedError


def scripted(setups: Mapping[datetime, Setup]) -> Scripted:
    playbook = Scripted()
    playbook.script = dict(setups)  # type: ignore[misc]
    return playbook


def setup(
    *,
    direction: Direction = Direction.LONG,
    entry: str | None = "200",
    stop: str = "199",
    target: str | None = "203",
    time_stop: datetime | None = None,
    max_size: str = "1",
) -> Setup:
    return Setup(
        playbook=Scripted().tag,
        symbol="NVDA-USD",
        direction=direction,
        reference_price=D("200"),
        entry_price=None if entry is None else D(entry),
        stop_price=D(stop),
        target_price=None if target is None else D(target),
        time_stop=time_stop or AT + minutes(60),
        conviction=Conviction.NORMAL,
        max_size=D(max_size),
        reward_risk=None,
        reasons=("scripted",),
    )


def marks(changes: Mapping[datetime, Bar] | None = None) -> tuple[Bar, ...]:
    """Flat 200 marks from an hour before the open to after the window, with ``changes``."""
    start = T0 - minutes(65)
    count = int((WINDOW.end - start) / timedelta(minutes=1)) + 5
    bars = {b.start: replace(b, volume=D(0), notional=D(0)) for b in flat(start, count, wick="0")}
    bars.update(changes or {})
    return tuple(bars[s] for s in sorted(bars))


def day_data(
    *,
    mark_changes: Mapping[datetime, Bar] | None = None,
    candles: Sequence[Bar] = (),
    funding: Sequence[FundingRate] = (),
) -> DayData:
    return DayData(
        symbol="NVDA-USD",
        day=DAY,
        candles=tuple(candles),
        marks=marks(mark_changes),
        funding=tuple(funding),
    )


def mark(start: datetime, o: str, h: str, lo: str, c: str) -> Bar:
    return bar(start, o, h, lo, c, volume="0")


def run(playbook: Playbook[Any], data: DayData) -> BacktestResult:
    return Backtester(CONFIG, CALENDAR, [playbook]).run({"NVDA-USD": {DAY: data}}, [DAY])


def only(result: BacktestResult) -> BacktestTrade:
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


# --- Data ---


def test_mark_bars_take_each_minutes_first_high_low_and_last_mark() -> None:
    points = [
        MarkPoint(instrument_id=19, timestamp=T0 + timedelta(seconds=s), mark_price=D(p))
        for s, p in [(0, "200"), (20, "201"), (40, "199.5"), (59, "200.5"), (60, "202")]
    ]
    first, second = mark_bars(reversed(points))
    assert (first.start, first.open, first.high, first.low, first.close) == (
        T0, D("200"), D("201"), D("199.5"), D("200.5"),
    )  # fmt: skip
    assert first.volume == 0
    assert (second.start, second.open, second.close) == (T0 + minutes(1), D("202"), D("202"))


def test_feature_bars_prefer_the_trade_candle_of_a_traded_minute() -> None:
    traded = bar(T0, "200", "201", "199", "200.5", volume="3")
    data = day_data(candles=[traded])
    merged = {b.start: b for b in data.feature_bars()}
    assert merged[T0] == traded
    assert merged[T0 + minutes(1)].volume == 0


def test_cached_day_round_trips(tmp_path: Path) -> None:
    rate = FundingRate(instrument_id=19, rate=D("0.00000625"), timestamp=T0 + minutes(30))
    data = day_data(candles=[bar(T0, "200", "201", "199", "200.5")], funding=[rate])
    path = tmp_path / "NVDA-USD" / "2026-10-20.json"
    write_day(path, data)
    assert read_day(path) == data
    write_day(path, replace(data, marks=(), marks_loaded=False))
    loaded = read_day(path)
    assert loaded is not None and not loaded.marks_loaded
    path.write_text("{}")
    assert read_day(path) is None


def test_history_cache_fetches_once_and_only_complete_days(tmp_path: Path) -> None:
    fake = FakeExchange([instrument()])
    fake.candles[(19, "1m")] = [
        Candle(
            instrument_id=19, interval="1m", open_time=T0, open=D(200), high=D(201),
            low=D(199), close=D(200), volume=D(2), trade_count=1,
        ),
        Candle(
            instrument_id=19, interval="1m", open_time=T0 + minutes(1), open=D(200),
            high=D(200), low=D(200), close=D(200), volume=D(0), trade_count=0,
        ),
    ]  # fmt: skip
    fake.marks[(19, "1s")] = [
        MarkPoint(instrument_id=19, timestamp=T0 + timedelta(seconds=s), mark_price=D(200))
        for s in range(0, 120, 30)
    ]
    cache = HistoryCache(tmp_path, fake, {"NVDA-USD": 19}, CALENDAR)
    during = WINDOW.end
    loaded = asyncio.run(cache.day("NVDA-USD", DAY, marks=True, now=during))
    assert loaded is not None
    assert len(loaded.candles) == 1  # untraded candles are dropped
    assert [b.start for b in loaded.marks] == [T0, T0 + minutes(1)]
    assert not (tmp_path / "NVDA-USD" / "2026-10-20.json").exists()  # day not over

    after = WINDOW.market.close + minutes(1)
    asyncio.run(cache.day("NVDA-USD", DAY, marks=True, now=after))
    fake.marks.clear()
    again = asyncio.run(cache.day("NVDA-USD", DAY, marks=True, now=after))
    assert again == loaded
    assert asyncio.run(cache.day("NVDA-USD", date(2026, 10, 24), marks=True, now=after)) is None


def test_window_days_are_trading_days_whose_window_is_over() -> None:
    config = CONFIG.model_copy(update={"backtest": BacktestConfig(window_days=7)})
    days = window_days(config, CALENDAR, WINDOW.end)
    assert days == [date(2026, 10, d) for d in (13, 14, 15, 16, 19)]
    days = window_days(config, CALENDAR, WINDOW.end + minutes(5))
    assert days[-1] == DAY


# --- Fill model ---


def test_limit_entry_fills_only_through_the_limit_then_takes_the_target() -> None:
    playbook = scripted({AT: setup()})
    data = day_data(
        candles=[
            bar(AT, "200", "200.1", "200", "200", volume="1"),  # touches only
            bar(AT + minutes(1), "200", "200.1", "199.99", "200", volume="1"),
        ],
        mark_changes={AT + minutes(5): mark(AT + minutes(5), "201", "203.5", "201", "203")},
    )
    trade = only(run(playbook, data))
    assert trade.entry_at == AT + minutes(1)
    assert trade.entry_price == D(200)
    assert not trade.marketable
    assert trade.exit_reason is ExitReason.TARGET
    assert trade.exit_at == AT + minutes(5)
    assert trade.exit_price == D(203) * (1 - SLIP)
    per_unit = D(1) + D(200) * MAKER + D(199) * (TAKER + SLIP)
    assert trade.quantity == BUDGET / per_unit
    assert abs(trade.risk - BUDGET) < D("1e-20")
    assert trade.fees == trade.quantity * (D(200) * MAKER + trade.exit_price * TAKER)
    assert trade.gross_pnl == (trade.exit_price - 200) * trade.quantity
    assert trade.net_r > 2


def test_unfilled_limit_is_cancelled_after_the_entry_timeout() -> None:
    playbook = scripted({AT: setup()})
    late = bar(AT + minutes(2), "200", "200", "199", "199.5", volume="1")
    result = run(playbook, day_data(candles=[late]))
    assert result.trades == []
    assert result.tallies[playbook.tag] == PlaybookTally(signals=1, unfilled=1)


def test_a_bar_reaching_stop_and_target_hits_the_stop() -> None:
    playbook = scripted({AT: setup(entry=None)})
    wide = mark(AT + minutes(3), "200", "204", "198", "200")
    trade = only(run(playbook, day_data(mark_changes={AT + minutes(3): wide})))
    assert trade.marketable
    assert trade.entry_price == D(200) * (1 + D("0.0003"))
    assert trade.exit_reason is ExitReason.STOP
    assert trade.exit_price == D(199) * (1 - SLIP)
    assert abs(trade.net_r + 1) < D("0.001")  # sized for the slipped fill: a planned 1R


def test_a_stop_gapped_through_fills_at_the_open() -> None:
    playbook = scripted(
        {AT: setup(direction=Direction.SHORT, entry=None, stop="201", target="197")}
    )
    gap = mark(AT + minutes(2), "202", "202.5", "201.5", "202")
    trade = only(run(playbook, day_data(mark_changes={AT + minutes(2): gap})))
    assert trade.entry_price == D(200) * (1 - D("0.0003"))
    assert trade.exit_reason is ExitReason.STOP
    assert trade.exit_price == D(202) * (1 + SLIP)


def test_limit_fill_bar_checks_only_the_stop() -> None:
    playbook = scripted({AT: setup()})
    through = bar(AT, "200", "200", "199.9", "200", volume="1")
    both = mark(AT, "200", "203.5", "199.9", "200")
    trade = only(run(playbook, day_data(candles=[through], mark_changes={AT: both})))
    # The target was reached in the fill bar, but only bars after it count.
    assert trade.exit_reason is ExitReason.TIME_STOP


def test_time_stop_exits_at_the_minutes_first_mark() -> None:
    playbook = scripted({AT: setup(entry=None, time_stop=AT + minutes(10))})
    moved = mark(AT + minutes(10), "200.5", "200.6", "200.4", "200.5")
    trade = only(run(playbook, day_data(mark_changes={AT + minutes(10): moved})))
    assert trade.exit_reason is ExitReason.TIME_STOP
    assert trade.exit_at == AT + minutes(10)
    assert trade.exit_price == D("200.5") * (1 - SLIP)


def test_window_end_closes_what_is_still_open() -> None:
    playbook = scripted({AT: setup(entry=None, target=None, time_stop=WINDOW.end + minutes(30))})
    trade = only(run(playbook, day_data()))
    assert trade.exit_reason is ExitReason.WINDOW_END
    assert trade.exit_at == WINDOW.end


def test_a_setup_is_taken_again_only_after_it_went_away() -> None:
    quick = setup(entry=None, time_stop=AT + minutes(2))
    showing = {AT + minutes(i): quick for i in range(6)}
    showing[AT + minutes(8)] = replace(quick, time_stop=AT + minutes(10))
    result = run(scripted(showing), day_data())
    assert [t.signal_at for t in result.trades] == [AT, AT + minutes(8)]


def test_funding_is_paid_by_longs_when_the_rate_is_positive() -> None:
    hour = T0 + minutes(90)  # 15:00 UTC settlement
    rate = FundingRate(instrument_id=19, rate=D("0.0001"), timestamp=hour + timedelta(seconds=1))
    playbook = scripted({AT: setup(entry=None, target=None)})
    trade = only(run(playbook, day_data(funding=[rate])))
    assert trade.exit_reason is ExitReason.TIME_STOP
    assert trade.funding == -trade.quantity * D(200) * D("0.0001")
    assert trade.net_pnl == trade.gross_pnl - trade.fees + trade.funding


def test_trades_carry_the_sessions_regime_at_the_window_end() -> None:
    playbook = scripted({AT: setup(entry=None)})
    result = run(playbook, day_data())
    trade = only(result)
    assert result.sessions == [SessionInfo("NVDA-USD", DAY, trade.session_regime)]


def test_backtester_refuses_without_capital() -> None:
    with pytest.raises(ValueError, match="starting_collateral"):
        Backtester(CONFIG.model_copy(update={"sim": SimConfig()}), CALENDAR, [])


# --- Gate 1 ---

TREND = Regime(VolatilityRegime.NORMAL, StructureRegime.TREND_UP, D(1))
ROTATION = Regime(VolatilityRegime.NORMAL, StructureRegime.ROTATION, D(1))
MOMENTUM = next(p for p in load_playbooks() if p.id == "momentum_scalp")


def trade(net: str, *, regime: Regime = TREND, entry: Regime = TREND, n: int = 0) -> BacktestTrade:
    return BacktestTrade(
        playbook=MOMENTUM.tag,
        symbol="NVDA-USD",
        day=DAY,
        direction=Direction.LONG,
        variant=None,
        conviction="normal",
        reasons=("breakout",),
        signal_at=AT + minutes(n),
        entry_at=AT + minutes(n),
        entry_price=D(200),
        marketable=True,
        stop_price=D(199),
        target_price=D(203),
        time_stop=AT + minutes(n + 10),
        quantity=D(1),
        risk=D(1),
        exit_at=AT + minutes(n + 5),
        exit_price=D(200) + D(net),
        exit_reason=ExitReason.TARGET,
        gross_pnl=D(net),
        fees=D(0),
        funding=D(0),
        entry_regime=entry,
        session_regime=regime,
    )


def result_of(trades: Sequence[BacktestTrade]) -> BacktestResult:
    sessions = [SessionInfo("NVDA-USD", DAY, TREND), SessionInfo("TSLA-USD", DAY, ROTATION)]
    return BacktestResult(
        start=DAY,
        end=DAY,
        playbooks=(MOMENTUM,),
        trades=list(trades),
        tallies={MOMENTUM.tag: PlaybookTally(signals=len(trades))},
        sessions=sessions,
    )


def criteria(gate: Any) -> dict[str, bool]:
    return {c.name: c.passed for c in gate.criteria}


def test_gate1_passes_positive_spread_out_in_target_results() -> None:
    trades = [trade("1", n=i) for i in range(5)] + [trade("-5", regime=ROTATION, n=9)]
    gate = evaluate(MOMENTUM, result_of(trades), BacktestConfig(), T0)
    assert gate.passed, gate.criteria
    assert gate.target.trades == 5 and gate.other.trades == 1
    assert gate.target_sessions == 1 and gate.sessions == 2
    assert gate.max_trade_share == D("0.2")
    assert gate.target.expectancy_r == 1


def test_gate1_fails_negative_expectancy_concentration_and_no_trades() -> None:
    losing = evaluate(MOMENTUM, result_of([trade("1"), trade("-2", n=1)]), BacktestConfig(), T0)
    assert criteria(losing) == {
        "fires": True, "expectancy": False, "concentration": True, "regime_filter": True,
    }  # fmt: skip
    lumpy = evaluate(MOMENTUM, result_of([trade("4"), trade("1", n=1)]), BacktestConfig(), T0)
    assert criteria(lumpy)["concentration"] is False
    assert not lumpy.passed
    empty = evaluate(MOMENTUM, result_of([trade("3", regime=ROTATION)]), BacktestConfig(), T0)
    assert criteria(empty) == {
        "fires": False, "expectancy": False, "concentration": False, "regime_filter": True,
    }  # fmt: skip


def test_gate1_fails_an_entry_outside_the_declared_regimes() -> None:
    trades = [trade("1", n=i) for i in range(5)] + [trade("1", entry=ROTATION, n=7)]
    gate = evaluate(MOMENTUM, result_of(trades), BacktestConfig(), T0)
    assert criteria(gate)["regime_filter"] is False


def test_records_round_trip_and_feed_the_screened_versions(tmp_path: Path) -> None:
    passing = evaluate(
        MOMENTUM, result_of([trade("1", n=i) for i in range(5)]), BacktestConfig(), T0
    )
    failing = replace(passing, tag="vwap_reversion@9.deadbeef", passed=False)
    write_records(tmp_path, [passing, failing])
    records = read_records(tmp_path)
    assert records[MOMENTUM.tag]["verdict"] == "pass"
    assert records[MOMENTUM.tag]["review"] == "pending"
    assert len(records[MOMENTUM.tag]["sample"]) == 5
    assert records["vwap_reversion@9.deadbeef"]["verdict"] == "fail"
    (tmp_path / "gate1" / "broken.json").write_text("{")
    assert screened_playbooks(tmp_path, ["opening_flush@1.c8af2e86"]) == (
        MOMENTUM.tag,
        "opening_flush@1.c8af2e86",
    )
    assert screened_playbooks(tmp_path / "missing") == ()


def test_run_backtest_writes_report_and_records(tmp_path: Path) -> None:
    fake = FakeExchange([instrument()])
    fake.marks[(19, "1s")] = [
        MarkPoint(instrument_id=19, timestamp=T0 + minutes(i), mark_price=D(200))
        for i in range(-65, 185)
    ]
    playbook = scripted({AT: setup(entry=None)})
    config = CONFIG.model_copy(
        update={
            "backtest": BacktestConfig(window_days=7, data_dir=str(tmp_path / "data")),
            "agent": CONFIG.agent.model_copy(update={"results_dir": str(tmp_path / "results")}),
        }
    )
    run_ = asyncio.run(
        run_backtest(config, [playbook], now=WINDOW.market.close + minutes(1), source=fake)
    )
    assert run_.report == tmp_path / "results" / "backtests" / "2026-10-20" / "report.md"
    text = run_.report.read_text()
    assert "Playbook backtest 2026-10-13 to 2026-10-20" in text
    assert playbook.tag in text
    report = json.loads((run_.report.parent / "report.json").read_text())
    assert report["gate1"][0]["tag"] == playbook.tag
    trades = (run_.report.parent / "trades.jsonl").read_text().splitlines()
    assert len(trades) == 1 and json.loads(trades[0])["exit_reason"] == "time_stop"
    assert run_.records == (tmp_path / "results" / "gate1" / f"{playbook.tag}.json",)
