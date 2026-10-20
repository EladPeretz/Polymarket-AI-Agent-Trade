"""Live read-only test: record real market data, then replay a round trip through the
simulator. Skipped unless asked for:

    PERPS_AGENT_LIVE_TESTS=1 pytest tests/live

Public endpoints only; no order is sent anywhere.
"""

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from perps_agent.config.models import MarketDataConfig, SimConfig, VenueConfig
from perps_agent.market import MarketFeed, Recorder, read_recording
from perps_agent.sim import FillEvent, PaperSimulator, ReplayClock, replay
from perps_agent.venue import (
    InstrumentRegistry,
    OrderRequest,
    Side,
    TimeInForce,
    create_market_data,
)
from perps_agent.venue.rules import round_quantity

pytestmark = pytest.mark.skipif(
    os.environ.get("PERPS_AGENT_LIVE_TESTS") != "1",
    reason="live venue tests run only with PERPS_AGENT_LIVE_TESTS=1",
)

SYMBOL = os.environ.get("PERPS_AGENT_LIVE_SYMBOL", "NVDA-USD")


def test_round_trip_replays_against_a_live_recording(tmp_path: Path) -> None:
    async def record():
        source = create_market_data(VenueConfig())
        try:
            registry = InstrumentRegistry(source)
            await registry.refresh()
            instrument = registry.by_symbol(SYMBOL)
            recorder = Recorder(tmp_path, {instrument.instrument_id: SYMBOL})
            feed = MarketFeed(source, [instrument], MarketDataConfig(), recorder=recorder)
            stop = asyncio.Event()
            task = asyncio.create_task(feed.run(stop))
            try:
                async with asyncio.timeout(30):
                    while feed.snapshot(SYMBOL).stale:
                        await asyncio.sleep(0.2)
                await asyncio.sleep(6)
            finally:
                stop.set()
                await task
                recorder.close()
            return instrument
        finally:
            await source.aclose()

    instrument = asyncio.run(record())
    (path,) = tmp_path.rglob("*.jsonl")
    records = list(read_recording(path))
    clock = ReplayClock(records[0].at)
    sim = PaperSimulator([instrument], SimConfig(starting_collateral=Decimal(10_000)), clock=clock)
    sent: list[str] = []

    def strategy(record, events):
        mark = sim._known(instrument.instrument_id).mark  # noqa: SLF001
        position = sim.position(instrument.instrument_id)
        if mark is None:
            return
        if not sent:
            quantity = round_quantity(instrument, Decimal(1_000) / mark)
            sent.append(_send(sim, instrument.instrument_id, Side.BUY, quantity))
        elif len(sent) == 1 and position is not None:
            sent.append(_send(sim, instrument.instrument_id, Side.SELL, position.quantity, True))

    fills = [
        e.fill for e in replay(sim, clock, records, on_record=strategy) if isinstance(e, FillEvent)
    ]
    assert {f.side for f in fills} == {Side.BUY, Side.SELL}
    assert sim.position(instrument.instrument_id) is None
    # A taker round trip costs at least both fees, and the spread on top.
    assert sim.account.collateral < Decimal(10_000) - sum(f.fee for f in fills) + Decimal("0.5")
    assert all(f.is_taker for f in fills)


def _send(
    sim: PaperSimulator, iid: int, side: Side, quantity: Decimal, reduce: bool = False
) -> str:
    coid = uuid.uuid4().hex
    sim.submit(
        OrderRequest(
            client_order_id=coid,
            instrument_id=iid,
            side=side,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce,
        )
    )
    return coid
