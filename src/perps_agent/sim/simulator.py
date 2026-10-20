"""Paper fill simulator (overview §3.9, ADR-0005): a simulated venue fed by market data.

There is no Perps testnet, so paper trading executes against this model of the
venue. It implements ``OrderGateway`` and ``AccountReader`` and is fed every
market event the engine sees (live in paper mode, a recording in a replay).
The model is deliberately conservative:

* **Latency.** An order reaches the venue ``latency`` after it is sent. Its
  marketable part fills against the first book published at or after that
  moment, never the book the decision was made on.
* **Marketable orders** walk that book level by level up to their limit (a
  market order's limit is the edge of the price band). Liquidity taken stays
  taken until the next book frame replaces the book. Unfilled IOC quantity
  expires; a FOK fills completely or not at all; a GTC rests the remainder.
* **Resting limits** fill only when a public trade prints through the price,
  at the limit price, up to the trade's quantity (shared across our orders).
* **Fees** at the configured tier rates: taker for marketable fills, maker for
  resting fills.
* **Funding** settles at each ``next_funding`` time on the ticker, at the last
  rate and mark published before it.
* **Position TP/SL** (one of each per instrument, as on the venue) trigger on
  mark, then close the whole position with a market order against the book,
  through the same latency.
* **Liquidation** of an isolated position when margin plus unrealized PnL falls
  below maintenance margin: it sweeps the book at once, paying the taker fee
  plus the instrument's liquidation fee. It is a safety assertion; it is logged
  as an error and must never happen in normal operation.

Venue-rule checks reuse ``perps_agent.venue.rules``. Not modelled yet: bracket
TP/SL on entry (the execution layer sets position TP/SL after the fill), cross
margin, order modification, and the candle-based model for backtests (WP13).
"""

import itertools
import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from perps_agent.clock import utc_now
from perps_agent.config.models import SimConfig
from perps_agent.sim.account import SimAccount, SimPosition
from perps_agent.sim.fills import Level, available, fee, trades_through, walk_book
from perps_agent.venue import (
    BboEvent,
    BookEvent,
    Fill,
    Instrument,
    MarketEvent,
    OrderBook,
    OrderRequest,
    OrderState,
    OrderStatus,
    RequestRejected,
    Side,
    Ticker,
    TickerEvent,
    TimeInForce,
    Trade,
    TradesEvent,
    UnknownInstrument,
)
from perps_agent.venue.models import AccountSummary, Position, UtcDatetime
from perps_agent.venue.rules import STATUS_FOR_VIOLATION, check_order

log = logging.getLogger(__name__)

ProtectionKind = Literal["stop_loss", "take_profit"]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OrderUpdate(_Event):
    type: Literal["order"] = "order"
    order: OrderState


class FillEvent(_Event):
    type: Literal["fill"] = "fill"
    fill: Fill
    realized_pnl: Decimal


class FundingPayment(_Event):
    type: Literal["funding"] = "funding"
    instrument_id: int
    timestamp: UtcDatetime
    rate: Decimal
    mark_price: Decimal
    position_quantity: Decimal
    # Received by the account; negative when paid.
    amount: Decimal


class ProtectionTriggered(_Event):
    type: Literal["protection"] = "protection"
    instrument_id: int
    timestamp: UtcDatetime
    kind: ProtectionKind
    trigger_price: Decimal
    mark_price: Decimal
    # The market close order the trigger sent.
    client_order_id: str


class Liquidation(_Event):
    type: Literal["liquidation"] = "liquidation"
    instrument_id: int
    timestamp: UtcDatetime
    mark_price: Decimal
    position_quantity: Decimal
    margin_equity: Decimal
    maintenance_margin: Decimal


SimEvent = OrderUpdate | FillEvent | FundingPayment | ProtectionTriggered | Liquidation


class PositionProtection(_Event):
    """Position-level TP/SL, triggered by mark (venue research §10)."""

    stop_loss: Annotated[Decimal, Field(gt=0)] | None = None
    take_profit: Annotated[Decimal, Field(gt=0)] | None = None


@dataclass(slots=True)
class _Order:
    state: OrderState
    # When the order reaches the venue (sent + latency).
    active_at: datetime
    arrived: bool = False
    # A TP/SL close: keeps sweeping each new book until the position is flat.
    close_position: bool = False

    @property
    def request(self) -> OrderRequest:
        return self.state.request


@dataclass(slots=True)
class _Market:
    instrument: Instrument
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    book_time: datetime | None = None
    ticker: Ticker | None = None
    seen_trades: set[int] = field(default_factory=set)
    settled_funding: datetime | None = None
    leverage: Decimal | None = None
    protection: PositionProtection = PositionProtection()

    @property
    def mark(self) -> Decimal | None:
        return self.ticker.mark_price if self.ticker else None


class PaperSimulator:
    """A simulated venue. Satisfies ``OrderGateway`` and ``AccountReader``.

    Feed it every market event with :meth:`on_event` (and REST re-snapshots with
    :meth:`on_rest`). Each call returns the order updates, fills, funding
    payments, triggers and liquidations produced since the last call, oldest
    first, including the updates from orders sent or cancelled in between
    (:meth:`drain` returns them without new market data). ``clock`` gives the
    time an order is sent; replays pass a clock that follows the recording.
    """

    def __init__(
        self,
        instruments: Sequence[Instrument],
        config: SimConfig,
        *,
        starting_collateral: Decimal | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        collateral = (
            starting_collateral if starting_collateral is not None else config.starting_collateral
        )
        if collateral is None:
            raise ValueError("the simulator needs a starting collateral (sim.starting_collateral)")
        self._markets = {i.instrument_id: _Market(i) for i in instruments}
        self._config = config
        self._latency = config.latency_ms
        self._clock = clock
        self.account = SimAccount(collateral)
        self._orders: dict[str, _Order] = {}
        self._fills: list[Fill] = []
        self._order_ids = itertools.count(1)
        self._trade_ids = itertools.count(1)
        self._out: list[SimEvent] = []

    # --- Market input ---

    def on_event(self, event: MarketEvent) -> list[SimEvent]:
        match event:
            case BookEvent():
                market = self._markets.get(event.book.instrument_id)
                if market is not None:
                    self._set_book(market, event.book)
                    self._process_arrivals(market)
            case TickerEvent():
                market = self._markets.get(event.ticker.instrument_id)
                if market is not None:
                    self._set_ticker(market, event.ticker)
            case TradesEvent():
                if event.trades:
                    market = self._markets.get(event.trades[0].instrument_id)
                    if market is not None:
                        self._on_trades(market, event.trades)
            case BboEvent():
                pass  # the book carries everything the model needs
        return self.drain()

    def on_rest(self, book: OrderBook, ticker: Ticker, trades: Iterable[Trade]) -> list[SimEvent]:
        """A REST re-snapshot (after a gap): apply it like the matching stream events."""
        market = self._known(book.instrument_id)
        self._set_ticker(market, ticker)
        self._set_book(market, book)
        self._on_trades(market, tuple(trades))
        self._process_arrivals(market)
        return self.drain()

    def advance(self, now: datetime) -> list[SimEvent]:
        """Execute orders due by ``now`` against the current books, without a new frame.

        Only valid while the current books are known to still hold at ``now``: in a
        replay, between recorded frames while the feed is live (the recorder skips
        repeated frames). Live paper trading does not need it: books arrive every
        100 ms.
        """
        for market in self._markets.values():
            self._process_arrivals(market, now)
        return self.drain()

    def settle_funding(
        self, instrument_id: int, rate: Decimal, at: datetime, mark: Decimal | None = None
    ) -> list[SimEvent]:
        """Settle one funding interval explicitly (backtests driven by funding history)."""
        market = self._known(instrument_id)
        price = mark if mark is not None else market.mark
        if price is None:
            raise ValueError("no mark price to settle funding at")
        self._settle(market, rate, price, at)
        return self.drain()

    # --- OrderGateway ---

    async def place_order(self, request: OrderRequest) -> OrderState:
        return self.submit(request)

    async def cancel_order(self, client_order_id: str) -> OrderState:
        return self.cancel(client_order_id)

    async def cancel_all(self, instrument_id: int | None = None) -> tuple[OrderState, ...]:
        live = [
            coid
            for coid, order in self._orders.items()
            if order.state.status.is_live
            and (instrument_id is None or order.request.instrument_id == instrument_id)
        ]
        return tuple(self.cancel(coid) for coid in live)

    def submit(self, request: OrderRequest) -> OrderState:
        """Accept an order. It reaches the venue after the latency allowance."""
        if request.client_order_id in self._orders:
            raise RequestRejected("duplicate client order id", status=400, code="duplicate_order")
        market = self._known(request.instrument_id)
        now = self._clock()
        status = self._admission(market, request)
        order = _Order(
            state=OrderState(
                order_id=next(self._order_ids),
                request=request,
                status=status,
                filled_quantity=Decimal(0),
                resting_quantity=Decimal(0),
                created_at=now,
                updated_at=now,
            ),
            active_at=now + _ms(self._latency),
        )
        self._orders[request.client_order_id] = order
        self._out.append(OrderUpdate(order=order.state))
        return order.state

    def cancel(self, client_order_id: str) -> OrderState:
        order = self._orders.get(client_order_id)
        if order is None:
            raise RequestRejected("order not found", status=404, code="order_not_found")
        if order.state.status.is_live:
            self._finish(order, OrderStatus.CANCELLED, self._clock())
        return order.state

    # --- Position settings (the venue's leverage and position TP/SL) ---

    def set_leverage(self, instrument_id: int, leverage: Decimal) -> None:
        market = self._known(instrument_id)
        if not 1 <= leverage <= market.instrument.max_leverage:
            raise RequestRejected(f"leverage {leverage} out of range", status=400)
        market.leverage = leverage

    def set_protection(self, instrument_id: int, protection: PositionProtection) -> None:
        """Replace the position TP/SL. Needs an open position; flat clears it."""
        market = self._known(instrument_id)
        if self.account.position_quantity(instrument_id) == 0:
            raise RequestRejected("no open position to protect", status=400)
        market.protection = protection

    def protection(self, instrument_id: int) -> PositionProtection:
        return self._known(instrument_id).protection

    # --- AccountReader ---

    async def fetch_account(self) -> AccountSummary:
        return self.account_summary()

    async def fetch_positions(self) -> tuple[Position, ...]:
        return tuple(p.to_domain() for p in self.account.positions.values())

    async def fetch_open_orders(self) -> tuple[OrderState, ...]:
        return self.open_orders()

    async def fetch_fills(self, start: datetime, end: datetime) -> tuple[Fill, ...]:
        return tuple(f for f in self._fills if start <= f.timestamp <= end)

    def account_summary(self) -> AccountSummary:
        equity = self.account.equity(self._marks())
        return AccountSummary(
            equity=equity,
            available_margin=equity - self._initial_margin(),
            maker_fee_rate=self._config.maker_fee_rate,
            taker_fee_rate=self._config.taker_fee_rate,
        )

    def open_orders(self) -> tuple[OrderState, ...]:
        return tuple(o.state for o in self._orders.values() if o.state.status.is_live)

    def order(self, client_order_id: str) -> OrderState:
        return self._orders[client_order_id].state

    def position(self, instrument_id: int) -> SimPosition | None:
        return self.account.positions.get(instrument_id)

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    # --- Admission (at send time) ---

    def _admission(self, market: _Market, request: OrderRequest) -> OrderStatus:
        instrument = market.instrument
        if market.mark is None:
            return OrderStatus.MARK_PRICE_UNAVAILABLE
        for violation in check_order(instrument, request, market.mark):
            mapped = STATUS_FOR_VIOLATION.get(violation.rule)
            if mapped is None:
                raise RequestRejected(violation.detail, status=400, code=violation.rule)
            return mapped
        live = sum(
            1
            for o in self._orders.values()
            if o.state.status.is_live and o.request.instrument_id == instrument.instrument_id
        )
        if live >= instrument.max_order_count:
            return OrderStatus.RESTING_ORDER_LIMIT_EXCEEDED
        if request.reduce_only:
            if self._reducible(request) == 0:
                return OrderStatus.REDUCE_ONLY_INVALID
        elif self._initial_margin(request) > self.account.equity(self._marks()):
            raise RequestRejected("insufficient margin", status=400, code="insufficient_margin")
        return OrderStatus.ACCEPTED

    def _initial_margin(self, extra: OrderRequest | None = None) -> Decimal:
        """IM on each instrument's worst-case size, ``max(|pos + buys|, |pos − sells|)`` (§9).

        Resting orders consume margin. Reduce-only orders never add exposure.
        """
        total = Decimal(0)
        orders = [o.state for o in self._orders.values() if o.state.status.is_live]
        for iid, market in self._markets.items():
            position = self.account.positions.get(iid)
            mark = market.mark or (position.entry_price if position else None)
            if mark is None:
                continue
            quantity = position.quantity if position else Decimal(0)
            buys = sells = Decimal(0)
            requests = [(s.request, s.request.quantity - s.filled_quantity) for s in orders]
            if extra is not None:
                requests.append((extra, extra.quantity))
            for request, left in requests:
                if request.instrument_id != iid or request.reduce_only:
                    continue
                if request.side is Side.BUY:
                    buys += left
                else:
                    sells += left
            worst = max(abs(quantity + buys), abs(quantity - sells))
            total += worst * mark / self._leverage(market)
        return total

    def _reducible(self, request: OrderRequest) -> Decimal:
        """How much of a reduce-only order may still fill: what closes the position."""
        position = self.account.position_quantity(request.instrument_id)
        if position * request.side.sign >= 0:
            return Decimal(0)
        return abs(position)

    # --- Matching ---

    def _set_book(self, market: _Market, book: OrderBook) -> None:
        if market.book_time is not None and book.timestamp < market.book_time:
            return
        market.bids = [Level(lv.price, lv.quantity) for lv in book.bids]
        market.asks = [Level(lv.price, lv.quantity) for lv in book.asks]
        market.book_time = book.timestamp

    def _process_arrivals(self, market: _Market, now: datetime | None = None) -> None:
        """Execute orders that have reached the venue by ``now`` (default: the book's time)."""
        if market.book_time is None:
            return
        book_time = max(market.book_time, now) if now is not None else market.book_time
        iid = market.instrument.instrument_id
        due = [
            o
            for o in self._orders.values()
            if o.request.instrument_id == iid
            and o.state.status.is_live
            and o.active_at <= book_time
            and (not o.arrived or o.close_position)
        ]
        for order in sorted(due, key=lambda o: (o.active_at, o.state.order_id)):
            if order.state.status.is_live:
                self._arrive(market, order, book_time)

    def _arrive(self, market: _Market, order: _Order, now: datetime) -> None:
        request = order.request
        order.arrived = True
        assert market.mark is not None  # admission required a mark
        remaining = request.quantity - order.state.filled_quantity
        if request.reduce_only:
            remaining = min(remaining, self._reducible(request))
            if remaining == 0:
                status = (
                    OrderStatus.FILLED if order.close_position else OrderStatus.REDUCE_ONLY_INVALID
                )
                self._finish(order, status, now)
                return
        limit = request.price
        if limit is None:
            band = market.instrument.price_band
            limit = market.mark * (1 + band if request.side is Side.BUY else 1 - band)
        levels = market.asks if request.side is Side.BUY else market.bids
        if request.post_only:
            if available(levels, request.side, limit) > 0:
                self._finish(order, OrderStatus.POST_ONLY_REJECTED, now)
            else:
                self._rest(order, now)
            return
        if (
            request.time_in_force is TimeInForce.FOK
            and available(levels, request.side, limit) < remaining
        ):
            self._finish(order, OrderStatus.FOK_UNFILLED, now)
            return
        for execution in walk_book(levels, request.side, remaining, limit):
            self._fill(market, order, execution.price, execution.quantity, now, taker=True)
        if not order.state.status.is_live:
            return
        if order.close_position:
            return  # keeps sweeping new books until flat
        if request.time_in_force is TimeInForce.GTC:
            self._rest(order, now)
        else:
            # An IOC that filled in part (a FOK always fills in full here).
            filled = order.state.filled_quantity > 0
            self._finish(order, OrderStatus.IOC_EXPIRED if filled else OrderStatus.IOC_NO_FILL, now)

    def _on_trades(self, market: _Market, trades: Sequence[Trade]) -> None:
        iid = market.instrument.instrument_id
        new = [t for t in trades if t.trade_id not in market.seen_trades]
        market.seen_trades.update(t.trade_id for t in new)
        for trade in sorted(new, key=lambda t: (t.timestamp, t.trade_id)):
            left = trade.quantity
            resting = [
                o
                for o in self._orders.values()
                if o.request.instrument_id == iid
                and o.arrived
                and not o.close_position
                and o.state.resting_quantity > 0
                and o.active_at <= trade.timestamp
                and o.request.price is not None
                and trades_through(o.request.side, o.request.price, trade.price)
            ]
            # Best-priced first: the order furthest through the print fills first.
            resting.sort(
                key=lambda o: (
                    -(o.request.price or 0) * o.request.side.sign,
                    o.state.order_id,
                )
            )
            for order in resting:
                if left <= 0:
                    break
                assert order.request.price is not None
                take = min(left, order.state.resting_quantity)
                if order.request.reduce_only:
                    take = min(take, self._reducible(order.request))
                    if take == 0:
                        self._finish(order, OrderStatus.REDUCE_ONLY_EXPIRED, trade.timestamp)
                        continue
                self._fill(market, order, order.request.price, take, trade.timestamp, taker=False)
                left -= take

    def _rest(self, order: _Order, now: datetime) -> None:
        resting = order.request.quantity - order.state.filled_quantity
        status = OrderStatus.OPEN if order.state.filled_quantity == 0 else OrderStatus.PARTIAL
        self._update(order, status=status, resting_quantity=resting, updated_at=now)

    def _fill(
        self,
        market: _Market,
        order: _Order,
        price: Decimal,
        quantity: Decimal,
        now: datetime,
        *,
        taker: bool,
        liquidation: bool = False,
    ) -> None:
        rate = self._config.taker_fee_rate if taker else self._config.maker_fee_rate
        if liquidation:
            rate += market.instrument.liquidation_fee_rate
        cost = fee(price, quantity, rate)
        effect = self.account.apply_fill(
            market.instrument, order.request.side, price, quantity, cost, self._leverage(market)
        )
        fill = Fill(
            trade_id=next(self._trade_ids),
            order_id=order.state.order_id,
            client_order_id=order.request.client_order_id,
            instrument_id=market.instrument.instrument_id,
            side=order.request.side,
            price=price,
            quantity=quantity,
            fee=cost,
            is_taker=taker,
            timestamp=now,
            liquidation=liquidation,
        )
        self._fills.append(fill)
        filled = order.state.filled_quantity + quantity
        done = filled >= order.request.quantity
        # Taker fills happen before the order rests; maker fills come out of the resting size.
        resting = order.state.resting_quantity if taker else order.state.resting_quantity - quantity
        self._out.append(FillEvent(fill=fill, realized_pnl=effect.realized_pnl))
        self._update(
            order,
            filled_quantity=filled,
            resting_quantity=Decimal(0) if done else resting,
            status=OrderStatus.FILLED if done else OrderStatus.PARTIAL,
            updated_at=now,
        )
        self._after_position_change(market, now)

    def _after_position_change(self, market: _Market, now: datetime) -> None:
        iid = market.instrument.instrument_id
        if self.account.position_quantity(iid) != 0:
            return
        market.protection = PositionProtection()
        for order in list(self._orders.values()):
            if (
                order.request.instrument_id == iid
                and order.request.reduce_only
                and order.state.status.is_live
                and order.arrived
            ):
                status = (
                    OrderStatus.FILLED if order.close_position else OrderStatus.REDUCE_ONLY_EXPIRED
                )
                self._finish(order, status, now)

    # --- Mark-driven: funding, TP/SL, liquidation ---

    def _set_ticker(self, market: _Market, ticker: Ticker) -> None:
        previous = market.ticker
        if previous is not None and ticker.timestamp < previous.timestamp:
            return
        if (
            previous is not None
            and ticker.timestamp >= previous.next_funding
            and market.settled_funding != previous.next_funding
        ):
            self._settle(market, previous.funding_rate, previous.mark_price, previous.next_funding)
        market.ticker = ticker
        self._check_liquidation(market, ticker)
        self._check_protection(market, ticker)

    def _settle(self, market: _Market, rate: Decimal, mark: Decimal, at: datetime) -> None:
        market.settled_funding = at
        iid = market.instrument.instrument_id
        quantity = self.account.position_quantity(iid)
        amount = self.account.apply_funding(iid, mark, rate)
        if amount is not None:
            self._out.append(
                FundingPayment(
                    instrument_id=iid,
                    timestamp=at,
                    rate=rate,
                    mark_price=mark,
                    position_quantity=quantity,
                    amount=amount,
                )
            )

    def _check_protection(self, market: _Market, ticker: Ticker) -> None:
        iid = market.instrument.instrument_id
        quantity = self.account.position_quantity(iid)
        protection = market.protection
        if quantity == 0 or protection == PositionProtection():
            return
        mark = ticker.mark_price
        long = quantity > 0
        kind: ProtectionKind | None = None
        trigger = Decimal(0)
        if protection.stop_loss is not None and (
            mark <= protection.stop_loss if long else mark >= protection.stop_loss
        ):
            kind, trigger = "stop_loss", protection.stop_loss
        elif protection.take_profit is not None and (
            mark >= protection.take_profit if long else mark <= protection.take_profit
        ):
            kind, trigger = "take_profit", protection.take_profit
        if kind is None:
            return
        market.protection = PositionProtection()
        coid = uuid.uuid4().hex
        request = OrderRequest(
            client_order_id=coid,
            instrument_id=iid,
            side=Side.SELL if long else Side.BUY,
            quantity=abs(quantity),
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
        )
        now = ticker.timestamp
        order = _Order(
            state=OrderState(
                order_id=next(self._order_ids),
                request=request,
                status=OrderStatus.ACCEPTED,
                filled_quantity=Decimal(0),
                resting_quantity=Decimal(0),
                created_at=now,
                updated_at=now,
            ),
            active_at=now + _ms(self._latency),
            close_position=True,
        )
        self._orders[coid] = order
        self._out.append(
            ProtectionTriggered(
                instrument_id=iid,
                timestamp=now,
                kind=kind,
                trigger_price=trigger,
                mark_price=mark,
                client_order_id=coid,
            )
        )
        self._out.append(OrderUpdate(order=order.state))

    def _check_liquidation(self, market: _Market, ticker: Ticker) -> None:
        iid = market.instrument.instrument_id
        position = self.account.positions.get(iid)
        mark = ticker.mark_price
        if position is None or not position.is_liquidatable(mark):
            return
        now = ticker.timestamp
        event = Liquidation(
            instrument_id=iid,
            timestamp=now,
            mark_price=mark,
            position_quantity=position.quantity,
            margin_equity=position.margin_equity(mark),
            maintenance_margin=position.maintenance_margin(mark),
        )
        log.error(
            "simulated liquidation: a position fell below maintenance margin",
            extra={"symbol": market.instrument.symbol, "mark": str(mark)},
        )
        self._out.append(event)
        for order in list(self._orders.values()):
            if order.request.instrument_id == iid and order.state.status.is_live:
                self._finish(order, OrderStatus.CANCELLED, now)
        side = Side.SELL if position.quantity > 0 else Side.BUY
        quantity = abs(position.quantity)
        order = _Order(
            state=OrderState(
                order_id=next(self._order_ids),
                request=OrderRequest(
                    client_order_id=uuid.uuid4().hex,
                    instrument_id=iid,
                    side=side,
                    quantity=quantity,
                    time_in_force=TimeInForce.IOC,
                    reduce_only=True,
                ),
                status=OrderStatus.ACCEPTED,
                filled_quantity=Decimal(0),
                resting_quantity=Decimal(0),
                created_at=now,
                updated_at=now,
            ),
            active_at=now,
            arrived=True,
        )
        self._orders[order.request.client_order_id] = order
        levels = market.asks if side is Side.BUY else market.bids
        executions = walk_book(levels, side, quantity, None)
        swept = sum((e.quantity for e in executions), Decimal(0))
        for execution in executions:
            self._fill(
                market,
                order,
                execution.price,
                execution.quantity,
                now,
                taker=True,
                liquidation=True,
            )
        if swept < quantity:
            # The book could not absorb it: the rest closes at mark (the venue's
            # insurance fund would take it over).
            self._fill(market, order, mark, quantity - swept, now, taker=True, liquidation=True)

    # --- Helpers ---

    def _finish(self, order: _Order, status: OrderStatus, now: datetime) -> None:
        self._update(order, status=status, resting_quantity=Decimal(0), updated_at=now)

    def _update(self, order: _Order, **changes: object) -> None:
        order.state = order.state.model_copy(update=changes)
        self._out.append(OrderUpdate(order=order.state))

    def _leverage(self, market: _Market) -> Decimal:
        # The venue default with no explicit setting is isolated at max leverage (§5).
        return market.leverage or Decimal(market.instrument.max_leverage)

    def _marks(self) -> dict[int, Decimal]:
        return {iid: m.mark for iid, m in self._markets.items() if m.mark is not None}

    def _known(self, instrument_id: int) -> _Market:
        try:
            return self._markets[instrument_id]
        except KeyError:
            raise UnknownInstrument(f"simulator does not track {instrument_id}") from None

    def drain(self) -> list[SimEvent]:
        """Every event produced since the last drain, oldest first."""
        out, self._out = self._out, []
        return out


def _ms(value: int) -> timedelta:
    return timedelta(milliseconds=value)
