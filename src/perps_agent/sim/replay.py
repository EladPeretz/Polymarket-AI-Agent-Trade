"""Replay a market-data recording through the simulator.

Records are applied in file order. Stream events and REST re-snapshots feed the
simulator. The clock follows each record's receipt time, so orders sent from
``on_record`` see the same latency they would have had live.

The recorder writes a book or ticker frame only when it changed, so a quiet
market leaves seconds between records. While the feed was live, the last book
still held until the next record, so orders that came due in between execute
against it (``PaperSimulator.advance``). While it was stale (between a
``stale`` or ``resubscribing`` status and the next ``fresh``), nothing is
assumed and orders wait for the next book.
"""

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime

from perps_agent.market import RestRecord, StatusRecord, StreamRecord
from perps_agent.sim.simulator import PaperSimulator, SimEvent

Record = StreamRecord | RestRecord | StatusRecord


class ReplayClock:
    """A clock set by the replay, for :class:`PaperSimulator`'s ``clock``."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def replay(
    simulator: PaperSimulator,
    clock: ReplayClock,
    records: Iterable[Record],
    on_record: Callable[[Record, list[SimEvent]], None] | None = None,
) -> Iterator[SimEvent]:
    """Feed ``records`` to ``simulator`` and yield what it produces.

    ``on_record`` runs after each record is applied, with the events it caused;
    it may send or cancel orders (their immediate updates are yielded next).
    """
    live = True
    for record in records:
        if live:
            yield from simulator.advance(record.at)
        clock.now = max(clock.now, record.at)
        match record:
            case StreamRecord():
                events = simulator.on_event(record.event)
            case RestRecord():
                events = simulator.on_rest(record.book, record.ticker, record.trades)
            case StatusRecord():
                live = record.status in ("subscribed", "fresh")
                events = simulator.drain()
        yield from events
        if on_record is not None:
            on_record(record, events)
            yield from simulator.drain()
