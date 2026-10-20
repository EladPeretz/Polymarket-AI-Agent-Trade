from decimal import Decimal

from perps_agent.market import Recorder, read_recording
from perps_agent.sim import FillEvent, PaperSimulator, ReplayClock, replay
from perps_agent.venue import Side
from tests.unit.sim_factories import CONFIG, at, book_event, ticker_event
from tests.unit.venue_factories import T0, instrument, order


def _record(tmp_path, *items):
    recorder = Recorder(tmp_path, {19: "NVDA-USD"})
    for item in items:
        if isinstance(item, tuple):
            recorder.status(19, item[0], at(item[1]))
        else:
            recorder.stream(item, 19)
    recorder.close()
    (path,) = tmp_path.rglob("*.jsonl")
    return read_recording(path)


def _replay_buy(records):
    clock = ReplayClock(T0)
    sim = PaperSimulator([instrument()], CONFIG, clock=clock)
    sent = []

    def strategy(record, events):
        if record.type == "stream" and record.event.kind == "book" and not sent:
            sent.append(sim.submit(order("1", price=None)))

    events = list(replay(sim, clock, records, on_record=strategy))
    return sim, sent, [e.fill for e in events if isinstance(e, FillEvent)]


def test_order_fills_on_the_book_that_held_when_it_arrived(tmp_path):
    records = _record(
        tmp_path,
        ("subscribed", 0),
        ticker_event(0),
        book_event(0),
        # Sent at 0, arrives at 250: the 100 ms book still held then (the
        # recorder skips unchanged frames), not the one recorded at 400.
        book_event(100, asks=(("200.3", "5"),)),
        book_event(400, asks=(("200.5", "5"),)),
    )
    sim, sent, fills = _replay_buy(records)
    assert sent[0].created_at == at(0)
    (fill,) = fills
    assert (fill.price, fill.side, fill.timestamp) == (Decimal("200.3"), Side.BUY, at(400))
    assert sim.position(19).quantity == 1


def test_nothing_is_assumed_while_the_feed_is_stale(tmp_path):
    records = _record(
        tmp_path,
        ("subscribed", 0),
        ticker_event(0),
        book_event(0),
        ("stale", 100),
        book_event(3_000, asks=(("201", "5"),)),
    )
    _, _, fills = _replay_buy(records)
    (fill,) = fills
    assert fill.price == Decimal(201)
