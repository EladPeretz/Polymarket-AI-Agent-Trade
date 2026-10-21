import asyncio
from datetime import date
from decimal import Decimal

from perps_agent.calendar import NyseCalendar
from perps_agent.config.models import FeaturesConfig
from perps_agent.strategy import bar_from_candle, load_history, prior_sessions, summarize_sessions
from perps_agent.venue import Candle
from perps_agent.venue.fake import FakeExchange
from tests.unit.strategy_factories import minutes
from tests.unit.venue_factories import instrument

D = Decimal
EXCHANGE = NyseCalendar()


def _candles(day: date, closes: list[str]) -> list[Candle]:
    market = EXCHANGE.trading_day(day)
    assert market is not None
    previous = D(closes[0])
    rows = []
    for i, close in enumerate(closes):
        c = D(close)
        rows.append(
            Candle(
                instrument_id=19,
                interval="1m",
                open_time=market.open + minutes(i),
                open=previous,
                high=max(previous, c),
                low=min(previous, c),
                close=c,
                volume=D(5),
                trade_count=5,
            )
        )
        previous = c
    return rows


def test_prior_sessions_skip_weekends_and_holidays():
    # Monday 2026-10-19 back to Thursday 2026-10-15; 2026-11-27 follows Thanksgiving.
    assert prior_sessions(EXCHANGE, date(2026, 10, 20), 3) == [
        date(2026, 10, 15),
        date(2026, 10, 16),
        date(2026, 10, 19),
    ]
    assert prior_sessions(EXCHANGE, date(2026, 11, 27), 1) == [date(2026, 11, 25)]


def test_load_history_summarizes_each_prior_session():
    venue = FakeExchange([instrument()])
    venue.candles[(19, "1m")] = _candles(date(2026, 10, 16), ["200", "202", "201"]) + _candles(
        date(2026, 10, 19), ["201", "199", "198"]
    )
    config = FeaturesConfig(history_sessions=3)
    history = asyncio.run(load_history(venue, 19, EXCHANGE, date(2026, 10, 20), config))
    # 2026-10-15 has no candles, so it is left out.
    assert [h.date for h in history] == [date(2026, 10, 16), date(2026, 10, 19)]
    assert (history[0].high, history[0].close) == (D(202), D(201))
    assert history[1].low == D(198)
    bars = [bar_from_candle(c) for c in venue.candles[(19, "1m")]]
    assert (
        summarize_sessions(bars, EXCHANGE, [date(2026, 10, 16), date(2026, 10, 19)], config)
        == history
    )
