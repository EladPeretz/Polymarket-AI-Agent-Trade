import asyncio
from decimal import Decimal

import pytest

from perps_agent.venue import InstrumentRegistry, UnknownInstrument, VenueError
from perps_agent.venue.fake import FakeExchange
from tests.unit.venue_factories import instrument


def test_lookup_and_change_detection() -> None:
    fake = FakeExchange([instrument()])
    registry = InstrumentRegistry(fake)

    first = asyncio.run(registry.refresh())
    assert first.added == ("NVDA-USD",)
    assert registry.by_symbol("NVDA-USD").instrument_id == 19
    assert registry.by_id(19).symbol == "NVDA-USD"
    assert registry.loaded_at is not None

    fake.instruments[19] = instrument(min_notional=Decimal(20))
    fake.instruments[6] = instrument(instrument_id=6, symbol="BTC-USD")
    change = asyncio.run(registry.refresh())
    assert change.changed == ("NVDA-USD",) and change.added == ("BTC-USD",)
    assert registry.by_symbol("NVDA-USD").min_notional == Decimal(20)

    del fake.instruments[6]
    assert asyncio.run(registry.refresh()).removed == ("BTC-USD",)
    with pytest.raises(UnknownInstrument):
        registry.by_symbol("BTC-USD")
    with pytest.raises(UnknownInstrument):
        registry.by_id(6)


def test_duplicate_symbols_are_rejected() -> None:
    fake = FakeExchange([instrument(), instrument(instrument_id=20)])
    with pytest.raises(VenueError):
        asyncio.run(InstrumentRegistry(fake).refresh())


def test_periodic_refresh_keeps_last_good_list_on_failure() -> None:
    fake = FakeExchange([instrument()])
    registry = InstrumentRegistry(fake)
    calls = 0
    original = fake.fetch_instruments

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise VenueError("down")
        return await original()

    fake.fetch_instruments = flaky  # type: ignore[method-assign]

    async def run() -> None:
        await registry.refresh()
        stop = asyncio.Event()
        task = asyncio.create_task(registry.run_refresh(0.01, stop))
        while calls < 3:
            await asyncio.sleep(0.005)
        stop.set()
        await task

    asyncio.run(run())
    assert registry.by_symbol("NVDA-USD").instrument_id == 19
