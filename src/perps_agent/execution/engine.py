"""Order manager (overview §3.8): turns risk verdicts and exit requests into orders,
books every outcome in the ledger, and keeps each position protected.

* **Entries** send exactly the order the risk engine approved (``RiskVerdict.order``),
  after setting the approved leverage. A limit entry still working after
  ``entry_timeout_seconds`` is cancelled.
* **Protection.** As soon as an entry or add fills, the position TP/SL is set to
  the approved stop (and target), and confirmed in the ledger. If it cannot be
  set, the position is closed. A position the ledger shows without a confirmed
  stop is closed on the next :meth:`ExecutionEngine.tick`.
* **Exits** are reduce-only IOC orders limited ``exit_slippage_bp`` through the
  mark. A close that does not fill completely is re-sent on every tick until
  the position is flat. Closing cancels the symbol's working entries.
* **Stops only tighten**: every stop change goes through
  ``RiskEngine.check_stop_change``. Targets may move or be removed.
* **Time stops** close the position when they pass.

Every order carries a fresh 32-hex client order id. Engine-run trailing rules
and exit plans (ADR-0009) build on :meth:`update_protection` and
:meth:`reduce_position` in later packages; the guardian's window close-out uses
:meth:`close_all`.
"""

import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from perps_agent.clock import to_utc, utc_now
from perps_agent.config.models import ExecutionConfig
from perps_agent.execution.backend import ExecutionBackend
from perps_agent.ledger import (
    DayStarted,
    FillBooked,
    FundingBooked,
    Ledger,
    LedgerOrder,
    LedgerPosition,
    OrderChanged,
    OrderFailed,
    OrderPurpose,
    OrderSent,
    ProtectionChanged,
)
from perps_agent.risk import RiskCheck, RiskEngine, RiskReason, RiskState, RiskVerdict
from perps_agent.sim import (
    FillEvent,
    FundingPayment,
    Liquidation,
    OrderUpdate,
    ProtectionTriggered,
    SimEvent,
)
from perps_agent.venue import (
    Instrument,
    OrderRequest,
    OrderState,
    Side,
    TimeInForce,
    UnknownInstrument,
    VenueError,
)
from perps_agent.venue.rules import round_price, round_quantity

log = logging.getLogger(__name__)

_EXITS = (OrderPurpose.EXIT, OrderPurpose.REDUCE, OrderPurpose.PROTECTION)
_OPENS = (OrderPurpose.ENTRY, OrderPurpose.ADD)


class ExecutionError(Exception):
    """A request execution cannot carry out (not approved, no position, ...)."""


class ExecutionEngine:
    def __init__(
        self,
        backend: ExecutionBackend,
        ledger: Ledger,
        risk: RiskEngine,
        instruments: Sequence[Instrument],
        config: ExecutionConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
        new_order_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._backend = backend
        self.ledger = ledger
        self._risk = risk
        self._by_symbol = {i.symbol: i for i in instruments}
        self._by_id = {i.instrument_id: i for i in instruments}
        self._config = config
        self._clock = clock
        self._new_order_id = new_order_id
        self._marks: dict[str, Decimal] = {}
        # Symbol → why it is being closed, until it is flat.
        self._closing: dict[str, str] = {}

    # --- Day and state ---

    async def start_day(self, day: date) -> None:
        """Reset the day counters and fix the equity the daily loss limit is based on."""
        account = await self._backend.fetch_account()
        self.ledger.record(DayStarted(at=self._now(), day=day, equity=account.equity))

    def update_marks(self, marks: Mapping[str, Decimal]) -> None:
        self._marks.update(marks)

    async def risk_state(self) -> RiskState:
        """The portfolio view for the risk engine, from the ledger and the venue's margin."""
        account = await self._backend.fetch_account()
        ledger = self.ledger
        return RiskState(
            equity=ledger.equity(self._marks),
            available_margin=account.available_margin,
            day_start_equity=ledger.day_start_equity,
            positions={p.symbol: p.risk(self._mark(p)) for p in ledger.open_positions},
            pending_entries=frozenset(
                o.sent.symbol for o in ledger.live_orders(purposes=(OrderPurpose.ENTRY,))
            ),
            trades_today=ledger.entries_today,
            l2_trades_today=ledger.l2_entries_today,
        )

    @property
    def closing(self) -> Mapping[str, str]:
        return dict(self._closing)

    # --- Entries ---

    async def enter(self, verdict: RiskVerdict) -> OrderState:
        """Send the order a verdict approved: a new position, or an add when ``verdict.adding``."""
        order = verdict.order
        if not verdict.approved or order is None:
            raise ExecutionError(f"verdict for {verdict.intent.symbol} is {verdict.decision}")
        intent = verdict.intent
        instrument = self._instrument(intent.symbol)
        if intent.symbol in self._closing:
            raise ExecutionError(f"{intent.symbol} is being closed")
        position = self.ledger.open_position(intent.symbol)
        if verdict.adding:
            if position is None or position.direction is not intent.direction:
                raise ExecutionError(f"no {intent.direction} position in {intent.symbol}")
            position_id, purpose = position.position_id, OrderPurpose.ADD
        else:
            if position is not None or self.ledger.live_orders(intent.symbol, _OPENS):
                raise ExecutionError(f"{intent.symbol} already has a position or entry")
            position_id, purpose = uuid.uuid4().hex, OrderPurpose.ENTRY
        if verdict.leverage is not None:
            await self._backend.set_leverage(instrument.instrument_id, Decimal(verdict.leverage))
        return await self._send(
            order,
            symbol=intent.symbol,
            position_id=position_id,
            purpose=purpose,
            reason=intent.thesis,
            level=intent.level,
            playbook=intent.playbook,
            stop_price=verdict.stop_price,
            target_price=verdict.target_price,
            time_stop=intent.time_stop,
            approved_risk=verdict.risk_budget,
            leverage=verdict.leverage,
        )

    # --- Exits ---

    async def close_position(self, symbol: str, reason: str) -> OrderState | None:
        """Exit the whole position now. Re-sent on each tick until flat; ``None`` if flat."""
        position = self.ledger.open_position(symbol)
        if position is None:
            await self._cancel(self.ledger.live_orders(symbol, _OPENS))
            return None
        self._closing.setdefault(symbol, reason)
        await self._cancel(self.ledger.live_orders(symbol, _OPENS))
        working = self.ledger.live_orders(symbol, (OrderPurpose.EXIT, OrderPurpose.PROTECTION))
        if working:
            return None
        return await self._exit(position, position.quantity, OrderPurpose.EXIT, reason)

    async def reduce_position(self, symbol: str, fraction: Decimal, reason: str) -> OrderState:
        """Partial exit of ``fraction`` (0–1] of the open quantity."""
        if not 0 < fraction <= 1:
            raise ExecutionError(f"fraction must be in (0, 1], got {fraction}")
        position = self._open(symbol)
        instrument = self._instrument(symbol)
        quantity = round_quantity(instrument, position.quantity * fraction)
        if quantity >= position.quantity or position.quantity - quantity < instrument.lot_size:
            state = await self.close_position(symbol, reason)
            if state is None:
                raise ExecutionError(f"{symbol} already has an exit working")
            return state
        if quantity <= 0:
            raise ExecutionError(f"{fraction} of {position.quantity} rounds to nothing")
        return await self._exit(position, quantity, OrderPurpose.REDUCE, reason)

    async def close_all(self, reason: str) -> None:
        """Cancel every working order and close every position (guardian, kill switch)."""
        await self._cancel(self.ledger.live_orders(purposes=_OPENS))
        for position in self.ledger.open_positions:
            await self.close_position(position.symbol, reason)

    # --- Protection ---

    async def update_protection(
        self,
        symbol: str,
        *,
        stop_price: Decimal | None = None,
        target_price: Decimal | None = None,
        remove_target: bool = False,
        reason: str,
    ) -> RiskReason | None:
        """Tighten the stop and/or move or remove the target. Returns why not, or ``None``."""
        position = self._open(symbol)
        if position.stop_price is None:
            raise ExecutionError(f"{symbol} has no confirmed stop to change")
        instrument = self._instrument(symbol)
        mark = self._mark(position)
        sign = position.direction.sign
        stop = position.stop_price
        if stop_price is not None:
            stop = round_price(instrument, stop_price, position.direction.side)
            refused = self._risk.check_stop_change(position.risk(mark), stop, mark)
            if refused is not None:
                return refused
        target = None if remove_target else position.target_price
        if target_price is not None:
            target = round_price(instrument, target_price)
            if (target - mark) * sign <= 0:
                return RiskReason(
                    check=RiskCheck.TARGET_SIDE,
                    detail=f"target {target} is not on the winning side of mark {mark}",
                )
        await self._set_protection(position, stop, target, reason)
        return None

    # --- Venue reports ---

    async def on_events(self, events: Iterable[SimEvent]) -> None:
        """Book order updates, fills and funding; protect what just filled."""
        to_protect: list[str] = []
        flattened: list[LedgerPosition] = []
        for event in events:
            match event:
                case ProtectionTriggered():
                    self._on_trigger(event)
                case OrderUpdate():
                    self._on_order(event.order)
                case FillEvent():
                    position = self._on_fill(event)
                    order = self.ledger.order(event.fill.client_order_id or "")
                    if position.is_open and order is not None and order.sent.purpose.opens:
                        to_protect.append(position.position_id)
                    if not position.is_open:
                        flattened.append(position)
                case FundingPayment():
                    self._on_funding(event)
                case Liquidation():
                    log.error(
                        "position liquidated",
                        extra={"instrument_id": event.instrument_id, "mark": str(event.mark_price)},
                    )
        for position in flattened:
            self._closing.pop(position.symbol, None)
            await self._cancel(
                o
                for o in self.ledger.live_orders(position.symbol, _OPENS)
                if o.sent.position_id == position.position_id
            )
        for position_id in dict.fromkeys(to_protect):
            await self._protect(position_id)

    async def tick(self, now: datetime | None = None) -> None:
        """Timers: entry timeouts, time stops, pending closes and unprotected positions."""
        now = to_utc(now) if now is not None else self._now()
        timeout = timedelta(seconds=self._config.entry_timeout_seconds)
        expired = [
            o
            for o in self.ledger.live_orders(purposes=_OPENS)
            if o.sent.time_in_force is TimeInForce.GTC and now - o.sent.at >= timeout
        ]
        await self._cancel(expired)
        for position in self.ledger.open_positions:
            symbol = position.symbol
            if symbol in self._closing:
                await self.close_position(symbol, self._closing[symbol])
            elif position.time_stop is not None and now >= position.time_stop:
                await self.close_position(symbol, "time stop")
            elif position.stop_price is None:
                log.error("position without a confirmed stop", extra={"symbol": symbol})
                await self.close_position(symbol, "no confirmed stop")
        for symbol in list(self._closing):
            if self.ledger.open_position(symbol) is None:
                del self._closing[symbol]

    async def mismatches(self) -> list[str]:
        """Differences between the ledger's open positions and the venue's (empty: none)."""
        venue = {p.instrument_id: p for p in await self._backend.fetch_positions()}
        problems = []
        for position in self.ledger.open_positions:
            held = venue.pop(position.instrument_id, None)
            signed = position.quantity * position.direction.sign
            if held is None:
                problems.append(f"{position.symbol}: ledger {signed}, venue flat")
            elif held.quantity != signed or held.entry_price != position.average_entry:
                problems.append(
                    f"{position.symbol}: ledger {signed} @ {position.average_entry}, "
                    f"venue {held.quantity} @ {held.entry_price}"
                )
        for held in venue.values():
            problems.append(
                f"{self._by_id[held.instrument_id].symbol}: ledger flat, venue {held.quantity}"
            )
        return problems

    # --- Internals ---

    async def _send(self, request: OrderRequest, **meta: Any) -> OrderState:
        self.ledger.record(
            OrderSent(
                at=self._now(),
                client_order_id=request.client_order_id,
                instrument_id=request.instrument_id,
                side=request.side,
                quantity=request.quantity,
                price=request.price,
                time_in_force=request.time_in_force,
                post_only=request.post_only,
                reduce_only=request.reduce_only,
                **meta,
            )
        )
        try:
            state = await self._backend.place_order(request)
        except VenueError as error:
            self.ledger.record(
                OrderFailed(
                    at=self._now(), client_order_id=request.client_order_id, error=str(error)
                )
            )
            raise
        self._on_order(state)
        return state

    async def _exit(
        self, position: LedgerPosition, quantity: Decimal, purpose: OrderPurpose, reason: str
    ) -> OrderState:
        instrument = self._instrument(position.symbol)
        side = position.direction.side.opposite
        mark = self._mark(position)
        slip = self._config.exit_slippage_bp / 10_000
        limit = mark * (1 - slip) if side is Side.SELL else mark * (1 + slip)
        request = OrderRequest(
            client_order_id=self._new_order_id(),
            instrument_id=instrument.instrument_id,
            side=side,
            quantity=quantity,
            price=round_price(instrument, limit, side),
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
        )
        return await self._send(
            request,
            symbol=position.symbol,
            position_id=position.position_id,
            purpose=purpose,
            reason=reason,
        )

    async def _cancel(self, orders: Iterable[LedgerOrder]) -> None:
        for order in list(orders):
            try:
                state = await self._backend.cancel_order(order.client_order_id)
            except VenueError as error:
                log.warning(
                    "cancel failed",
                    extra={"client_order_id": order.client_order_id, "error": str(error)},
                )
                continue
            self._on_order(state)

    async def _protect(self, position_id: str) -> None:
        position = self.ledger.position(position_id)
        if position is None or not position.is_open:
            return
        stop, target = position.planned_stop, position.planned_target
        if position.stop_price is not None and stop is not None:
            # Never loosen what is already confirmed.
            if (stop - position.stop_price) * position.direction.sign < 0:
                stop = position.stop_price
        if (stop, target) == (position.stop_price, position.target_price):
            return
        reason = "entry filled" if position.stop_price is None else "add filled"
        await self._set_protection(position, stop, target, reason)

    async def _set_protection(
        self,
        position: LedgerPosition,
        stop: Decimal | None,
        target: Decimal | None,
        reason: str,
    ) -> None:
        try:
            await self._backend.set_protection(position.instrument_id, stop, target)
        except VenueError as error:
            log.error(
                "could not set position protection; closing",
                extra={"symbol": position.symbol, "error": str(error)},
            )
            await self.close_position(position.symbol, "protection failed")
            return
        self.ledger.record(
            ProtectionChanged(
                at=self._now(),
                position_id=position.position_id,
                symbol=position.symbol,
                stop_price=stop,
                target_price=target,
                reason=reason,
            )
        )

    def _on_trigger(self, event: ProtectionTriggered) -> None:
        symbol = self._instrument_by_id(event.instrument_id).symbol
        position = self.ledger.open_position(symbol)
        if position is None:
            return
        self._register_external(event.client_order_id, position, OrderPurpose.PROTECTION, event)
        # The venue clears the TP/SL once it fires.
        self.ledger.record(
            ProtectionChanged(
                at=event.timestamp,
                position_id=position.position_id,
                symbol=symbol,
                stop_price=None,
                target_price=None,
                reason=f"{event.kind} triggered at mark {event.mark_price}",
            )
        )

    def _on_order(self, state: OrderState) -> None:
        coid = state.request.client_order_id
        order = self.ledger.order(coid)
        if order is None:
            return  # an order of the venue's own; its fills register it
        if (order.status, order.filled_quantity, order.resting_quantity) == (
            state.status,
            state.filled_quantity,
            state.resting_quantity,
        ):
            return
        self.ledger.record(
            OrderChanged(
                at=state.updated_at,
                client_order_id=coid,
                status=state.status,
                filled_quantity=state.filled_quantity,
                resting_quantity=state.resting_quantity,
            )
        )

    def _on_fill(self, event: FillEvent) -> LedgerPosition:
        fill = event.fill
        symbol = self._instrument_by_id(fill.instrument_id).symbol
        coid = fill.client_order_id or uuid.uuid4().hex
        order = self.ledger.order(coid)
        if order is None:
            position = self.ledger.open_position(symbol)
            if position is None:
                raise ExecutionError(f"fill {fill.trade_id} for unknown order with {symbol} flat")
            purpose = OrderPurpose.LIQUIDATION if fill.liquidation else OrderPurpose.PROTECTION
            self._register_external(coid, position, purpose, event)
            order = self.ledger.order(coid)
            assert order is not None
        self._marks.setdefault(symbol, fill.price)
        self.ledger.record(
            FillBooked(
                at=fill.timestamp,
                client_order_id=coid,
                position_id=order.sent.position_id,
                symbol=symbol,
                trade_id=fill.trade_id,
                side=fill.side,
                price=fill.price,
                quantity=fill.quantity,
                fee=fill.fee,
                is_taker=fill.is_taker,
                liquidation=fill.liquidation,
            )
        )
        position = self.ledger.position(order.sent.position_id)
        assert position is not None
        return position

    def _on_funding(self, event: FundingPayment) -> None:
        symbol = self._instrument_by_id(event.instrument_id).symbol
        position = self.ledger.open_position(symbol)
        if position is None:
            log.warning("funding for a flat instrument", extra={"symbol": symbol})
            return
        self.ledger.record(
            FundingBooked(
                at=event.timestamp,
                position_id=position.position_id,
                symbol=symbol,
                rate=event.rate,
                mark_price=event.mark_price,
                amount=event.amount,
            )
        )

    def _register_external(
        self,
        client_order_id: str,
        position: LedgerPosition,
        purpose: OrderPurpose,
        event: ProtectionTriggered | FillEvent,
    ) -> None:
        if isinstance(event, FillEvent):
            at, side, reason = event.fill.timestamp, event.fill.side, "venue order"
        else:
            at, side, reason = event.timestamp, position.direction.side.opposite, event.kind
        self.ledger.record(
            OrderSent(
                at=at,
                client_order_id=client_order_id,
                symbol=position.symbol,
                instrument_id=position.instrument_id,
                position_id=position.position_id,
                purpose=purpose,
                side=side,
                quantity=position.quantity,
                price=None,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
                reason=reason,
            )
        )

    def _open(self, symbol: str) -> LedgerPosition:
        position = self.ledger.open_position(symbol)
        if position is None:
            raise ExecutionError(f"no open position in {symbol}")
        return position

    def _mark(self, position: LedgerPosition) -> Decimal:
        return self._marks.get(position.symbol, position.average_entry)

    def _instrument(self, symbol: str) -> Instrument:
        try:
            return self._by_symbol[symbol]
        except KeyError:
            raise UnknownInstrument(f"execution does not trade {symbol!r}") from None

    def _instrument_by_id(self, instrument_id: int) -> Instrument:
        try:
            return self._by_id[instrument_id]
        except KeyError:
            raise UnknownInstrument(f"execution does not trade {instrument_id}") from None

    def _now(self) -> datetime:
        return to_utc(self._clock())
