"""Contract tests: the real adapter and the pinned SDK against recorded venue responses.

Every SDK call the adapter makes is exercised here. Expected values are read
from the fixtures, so re-recording them does not require editing the tests.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from perps_agent.clock import from_ms
from perps_agent.venue import (
    InstrumentRegistry,
    RateLimited,
    RequestRejected,
    SchemaMismatch,
    Side,
    VenueUnavailable,
)
from perps_agent.venue.models import InstrumentCategory
from tests.contract.conftest import load_rest

NVDA = 19
SP500 = 1
START = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


def _raw_instrument(symbol: str) -> dict:
    return next(i for i in load_rest("instruments.json") if i["symbol"] == symbol)


def _is_ms_utc(value: datetime) -> bool:
    return value.tzinfo is UTC and value.microsecond % 1000 == 0


def test_instruments_map_venue_parameters(make_source) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_instruments()

    instruments = {i.symbol: i for i in asyncio.run(run())}
    raw = _raw_instrument("NVDA-USD")
    nvda = instruments["NVDA-USD"]
    assert nvda.instrument_id == raw["instrument_id"] == NVDA
    assert nvda.category is InstrumentCategory.EQUITY
    assert nvda.price_decimals == raw["price_decimals"]
    assert nvda.tick_size == Decimal(1).scaleb(-raw["price_decimals"])
    assert nvda.lot_size == Decimal(1).scaleb(-raw["quantity_decimals"])
    assert nvda.price_band == Decimal(raw["price_bounds"])
    assert nvda.min_notional == Decimal(raw["min_notional"])
    assert nvda.max_market_notional == Decimal(raw["max_market_notional"])
    assert nvda.max_limit_notional == Decimal(raw["max_limit_notional"])
    assert nvda.max_leverage == raw["max_leverage"]
    assert nvda.maintenance_margin_rate == Decimal("0.5") / raw["max_leverage"]
    assert [(t.notional_floor, t.max_leverage) for t in nvda.risk_tiers] == [
        (Decimal(t["lower_bound"]), t["max_leverage"]) for t in raw["risk_tiers"]
    ]
    assert nvda.close_only is raw["close_only"]
    assert nvda.settled is False
    assert all(isinstance(v, Decimal) for v in (nvda.price_band, nvda.min_notional))


def test_ticker_lookup_returns_the_requested_instrument(make_source, venue) -> None:
    """Regression (venue research §14): the API ignores instrument_id on /tickers and the
    SDK's fetch_perps_ticker returns the first row, SP500. We must return NVDA."""
    rows = load_rest("tickers.json")
    assert rows[0]["instrument_id"] == SP500, "fixture must reproduce the bug"

    async def run():
        async with make_source() as source:
            return await source.fetch_ticker(NVDA)

    ticker = asyncio.run(run())
    raw = next(r for r in rows if r["instrument_id"] == NVDA)
    assert ticker.instrument_id == NVDA
    assert ticker.mark_price == Decimal(raw["mark_price"])
    assert ticker.index_price == Decimal(raw["index_price"])
    assert ticker.funding_rate == Decimal(raw["funding_rate"])
    assert ticker.timestamp == from_ms(raw["timestamp"])
    assert ticker.next_funding == from_ms(raw["next_funding"])
    stats = next(s for s in load_rest("statistics.json") if s["instrument_id"] == NVDA)
    assert ticker.volume_24h == Decimal(stats["volume"])
    assert venue.calls("/v1/info/tickers"), "tickers must come from the all-tickers endpoint"


def test_missing_ticker_is_a_schema_mismatch(make_source) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_ticker(999_999)

    with pytest.raises(SchemaMismatch):
        asyncio.run(run())


def test_book(make_source, venue) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_book(NVDA, depth=10)

    book = asyncio.run(run())
    raw = load_rest("book.json")
    assert book.instrument_id == NVDA
    assert book.sequence == raw["sequence"]
    assert book.timestamp == from_ms(raw["timestamp"])
    assert [(lvl.price, lvl.quantity) for lvl in book.bids] == [
        (Decimal(p), Decimal(q)) for p, q in raw["bids"]
    ]
    assert book.best_bid is not None and book.best_ask is not None
    assert book.best_bid.price < book.best_ask.price
    params = venue.calls("/v1/info/book")[0].url.params
    assert params["instrument_id"] == str(NVDA) and params["depth"] == "10"


def test_book_for_another_instrument_is_rejected(make_source, venue) -> None:
    body = {**load_rest("book.json"), "instrument_id": SP500}
    venue.overrides["/v1/info/book"] = lambda _: httpx.Response(200, json=body)

    async def run():
        async with make_source() as source:
            return await source.fetch_book(NVDA)

    with pytest.raises(SchemaMismatch):
        asyncio.run(run())


def test_trades_oldest_first_with_aggressor_side(make_source, venue) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_trades(NVDA, START, END)

    trades = asyncio.run(run())
    raw = load_rest("trades.json")["data"]
    assert len(trades) == len(raw)
    assert [t.timestamp for t in trades] == sorted(t.timestamp for t in trades)
    by_id = {r["trade_id"]: r for r in raw}
    for trade in trades:
        row = by_id[trade.trade_id]
        assert trade.aggressor is (Side.BUY if row["side"] == "long" else Side.SELL)
        assert trade.price == Decimal(row["price"])
        assert trade.quantity == Decimal(row["quantity"])
        assert _is_ms_utc(trade.timestamp)
    params = venue.calls("/v1/info/trades")[0].url.params
    assert params["start_timestamp"] == str(int(START.timestamp() * 1000))
    assert params["end_timestamp"] == str(int(END.timestamp() * 1000))


def test_candles(make_source) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_candles(NVDA, "1m", START, END)

    candles = asyncio.run(run())
    raw = load_rest("klines.json")["data"]
    assert len(candles) == len(raw)
    for candle, (ts, o, h, low, c, v, n) in zip(candles, raw, strict=True):
        assert candle.open_time == from_ms(ts)
        assert (candle.open, candle.high, candle.low, candle.close) == tuple(
            map(Decimal, (o, h, low, c))
        )
        assert candle.volume == Decimal(v) and candle.trade_count == n
        assert candle.interval == "1m" and candle.instrument_id == NVDA


def test_funding_history_oldest_first(make_source) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_funding_history(NVDA, START, END)

    rates = asyncio.run(run())
    raw = load_rest("funding.json")["data"]
    assert len(rates) == len(raw) > 0
    assert [r.timestamp for r in rates] == sorted(from_ms(r["timestamp"]) for r in raw)
    assert {r.rate for r in rates} == {Decimal(r["funding_rate"]) for r in raw}


def test_mark_history(make_source) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_mark_history(NVDA, "1m", START, END)

    points = asyncio.run(run())
    raw = load_rest("mark_history.json")["data"]
    assert [(p.timestamp, p.mark_price) for p in points] == [
        (from_ms(ts), Decimal(price)) for ts, price in raw
    ]


def test_mark_history_follows_more_pages(make_source, venue) -> None:
    pages = [
        {"data": [[60_000, "1.0"], [120_000, "1.1"]], "more": True},
        {"data": [[180_000, "1.2"]], "more": False},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[len(venue.calls("/v1/info/mark-history")) - 1])

    venue.overrides["/v1/info/mark-history"] = handler

    async def run():
        async with make_source() as source:
            return await source.fetch_mark_history(NVDA, "1m", from_ms(0), from_ms(240_000))

    points = asyncio.run(run())
    assert [p.mark_price for p in points] == [Decimal("1.0"), Decimal("1.1"), Decimal("1.2")]
    second = venue.calls("/v1/info/mark-history")[1].url.params
    assert second["start_timestamp"] == "180000"


def test_exchange_status(make_source) -> None:
    async def run():
        async with make_source() as source:
            return await source.fetch_exchange_status()

    status = asyncio.run(run())
    raw = load_rest("exchange.json")
    assert status.cancel_only is raw["cancel_only"]
    assert status.engine_version == raw["engine_version"]


def test_registry_loads_from_the_adapter(make_source) -> None:
    async def run():
        async with make_source() as source:
            registry = InstrumentRegistry(source)
            await registry.refresh()
            return registry

    registry = asyncio.run(run())
    assert registry.by_symbol("NVDA-USD").instrument_id == NVDA
    assert registry.by_id(SP500).symbol == "SP500-USD"


# --- errors and retries ---


def _always(status: int, body: dict | None = None, headers: dict | None = None):
    return lambda _: httpx.Response(status, json=body or {}, headers=headers)


def test_rate_limit_is_retried_then_raised(make_source, venue) -> None:
    venue.overrides["/v1/info/exchange"] = _always(
        429, {"status": "err", "error": "ip_rate_limited"}
    )

    async def run():
        async with make_source() as source:
            return await source.fetch_exchange_status()

    with pytest.raises(RateLimited):
        asyncio.run(run())
    assert len(venue.calls("/v1/info/exchange")) == 3


def test_sdk_rate_limit_is_translated(make_source, venue) -> None:
    venue.overrides["/v1/info/instruments"] = _always(429, headers={"retry-after": "0"})

    async def run():
        async with make_source() as source:
            return await source.fetch_instruments()

    with pytest.raises(RateLimited):
        asyncio.run(run())
    assert len(venue.calls("/v1/info/instruments")) == 3


def test_unavailable_then_recovers(make_source, venue) -> None:
    def flaky(request: httpx.Request) -> httpx.Response:
        if len(venue.calls("/v1/info/book")) == 1:
            return httpx.Response(503, json={"status": "err", "error": "ticker_stale"})
        return httpx.Response(200, json=load_rest("book.json"))

    venue.overrides["/v1/info/book"] = flaky

    async def run():
        async with make_source() as source:
            return await source.fetch_book(NVDA)

    assert asyncio.run(run()).instrument_id == NVDA
    assert len(venue.calls("/v1/info/book")) == 2


def test_network_failure_is_unavailable(make_source, venue) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    venue.overrides["/v1/info/instruments"] = broken

    async def run():
        async with make_source() as source:
            return await source.fetch_instruments()

    with pytest.raises(VenueUnavailable):
        asyncio.run(run())


def test_client_error_is_not_retried(make_source, venue) -> None:
    venue.overrides["/v1/info/book"] = _always(400, {"status": "err", "error": "bad_request"})

    async def run():
        async with make_source() as source:
            return await source.fetch_book(NVDA)

    with pytest.raises(RequestRejected) as raised:
        asyncio.run(run())
    assert raised.value.status == 400
    assert len(venue.calls("/v1/info/book")) == 1


def test_shape_change_is_a_schema_mismatch(make_source, venue) -> None:
    rows = load_rest("instruments.json")
    del rows[0]["min_notional"]
    venue.overrides["/v1/info/instruments"] = lambda _: httpx.Response(200, json=rows)

    async def run():
        async with make_source() as source:
            return await source.fetch_instruments()

    with pytest.raises(SchemaMismatch):
        asyncio.run(run())
    assert len(venue.calls("/v1/info/instruments")) == 1


def test_only_reads_are_ever_sent(make_source, venue) -> None:
    """Nothing in the adapter signs or sends an order: every request is a public GET."""

    async def run():
        async with make_source() as source:
            await source.fetch_exchange_status()
            await source.fetch_instruments()
            await source.fetch_ticker(NVDA)
            await source.fetch_book(NVDA)
            await source.fetch_trades(NVDA, START, END)
            await source.fetch_candles(NVDA, "1m", START, END)
            await source.fetch_mark_history(NVDA, "1m", START, END)
            await source.fetch_funding_history(NVDA, START, END)

    asyncio.run(run())
    assert venue.requests
    for request in venue.requests:
        assert request.method == "GET"
        assert request.url.host == "api.perpetuals.polymarket.com"
        assert not {"polymarket-proxy", "polymarket-secret"} & set(request.headers)
