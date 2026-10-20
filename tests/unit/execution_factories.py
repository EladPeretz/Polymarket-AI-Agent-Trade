"""A paper desk for execution tests: simulator, ledger, risk and execution on one clock."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from itertools import count
from typing import Any

from perps_agent.config.models import ExecutionConfig, SimConfig
from perps_agent.execution import ExecutionEngine, SimBackend
from perps_agent.ledger import Ledger
from perps_agent.risk import RiskVerdict, TradeIntent
from perps_agent.sim import PaperSimulator, ReplayClock, SimEvent
from perps_agent.venue import (
    BookEvent,
    BookLevel,
    OrderBook,
    Side,
    Ticker,
    TickerEvent,
    Trade,
    TradesEvent,
)
from tests.unit.risk_factories import NOW, engine, snapshot
from tests.unit.venue_factories import instrument

D = Decimal
_trade_ids = count(1)


class Desk:
    def __init__(self, collateral: str = "500", backend: Any = None, **config: Any) -> None:
        self.clock = ReplayClock(NOW)
        self.sim = PaperSimulator(
            [instrument()],
            SimConfig(latency_ms=250, starting_collateral=D(collateral)),
            clock=self.clock,
        )
        self.ledger = Ledger(D(collateral))
        self.risk = engine()
        self.execution = ExecutionEngine(
            backend or SimBackend(self.sim),
            self.ledger,
            self.risk,
            [instrument()],
            ExecutionConfig(**config),
            clock=self.clock,
        )
        self.bid, self.ask, self.mark = "199.98", "200.02", "200"
        self.next_funding = NOW + timedelta(hours=1)

    @property
    def ms(self) -> int:
        return (self.clock.now - NOW) // timedelta(milliseconds=1)

    def run(self, coroutine: Any) -> Any:
        return asyncio.run(coroutine)

    def feed(self, events: list[SimEvent]) -> list[SimEvent]:
        self.run(self.execution.on_events(events))
        return events

    def market(
        self,
        ms: int,
        *,
        mark: str | None = None,
        bid: str | None = None,
        ask: str | None = None,
        quantity: str = "20",
        rate: str = "0.00000625",
    ) -> list[SimEvent]:
        """Advance to ``ms`` and publish a ticker and a book there."""
        self.mark, self.bid, self.ask = mark or self.mark, bid or self.bid, ask or self.ask
        self.clock.now = NOW + timedelta(milliseconds=ms)
        at = self.clock.now
        ticker = Ticker(
            instrument_id=19,
            mark_price=D(self.mark),
            index_price=D(self.mark),
            last_price=D(self.mark),
            mid_price=D(self.mark),
            open_interest=D(1000),
            funding_rate=D(rate),
            next_funding=self.next_funding,
            timestamp=at,
        )
        book = OrderBook(
            instrument_id=19,
            bids=(
                BookLevel(price=D(self.bid), quantity=D(quantity)),
                BookLevel(price=D(self.bid) - 1, quantity=D(50)),
            ),
            asks=(
                BookLevel(price=D(self.ask), quantity=D(quantity)),
                BookLevel(price=D(self.ask) + 1, quantity=D(50)),
            ),
            timestamp=at,
            sequence=ms,
        )
        events = self.sim.on_event(
            TickerEvent(channel="tickers::19", sequence=ms, received_at=at, ticker=ticker)
        )
        events += self.sim.on_event(
            BookEvent(channel="book::19", sequence=ms, received_at=at, book=book)
        )
        self.execution.update_marks({"NVDA-USD": D(self.mark)})
        return self.feed(events)

    def trade(self, ms: int, price: str, quantity: str = "100") -> list[SimEvent]:
        self.clock.now = NOW + timedelta(milliseconds=ms)
        event = TradesEvent(
            channel="trades::19",
            sequence=ms,
            received_at=self.clock.now,
            trades=(
                Trade(
                    trade_id=next(_trade_ids),
                    instrument_id=19,
                    aggressor=Side.SELL,
                    price=D(price),
                    quantity=D(quantity),
                    timestamp=self.clock.now,
                ),
            ),
        )
        return self.feed(self.sim.on_event(event))

    def evaluate(self, intent: TradeIntent, *, adding: bool = False) -> RiskVerdict:
        state = self.run(self.execution.risk_state())
        check = self.risk.evaluate_add if adding else self.risk.evaluate
        return check(
            intent,
            instrument=instrument(),
            snapshot=snapshot(bid=self.bid, ask=self.ask, mark=self.mark, taken_at=self.clock.now),
            state=state,
            now=self.clock.now,
        )

    def enter(self, intent: TradeIntent, *, adding: bool = False) -> RiskVerdict:
        verdict = self.evaluate(intent, adding=adding)
        assert verdict.approved, verdict.reasons
        self.run(self.execution.enter(verdict))
        self.feed(self.sim.drain())
        return verdict

    def tick(self) -> None:
        self.run(self.execution.tick())
        self.feed(self.sim.drain())

    def drain(self) -> None:
        self.feed(self.sim.drain())
