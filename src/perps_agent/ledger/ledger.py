"""Event-sourced portfolio ledger (overview §3.11).

Every state change is a recorded :mod:`~perps_agent.ledger.events` event, and
everything here (positions, PnL, fees, funding, day counters) is derived from
them. :meth:`Ledger.from_events` rebuilds the same state from a saved log.

Accounting matches the simulator's isolated-margin account (``sim.account``):
a fill in the position's direction moves the average entry; an opposite fill
realizes ``closed × (price − average) × sign``. ``collateral = starting +
realized − fees + funding`` and ``equity = collateral + unrealized PnL``.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from perps_agent.ledger.events import (
    DayStarted,
    FillBooked,
    FundingBooked,
    LedgerEvent,
    OrderChanged,
    OrderFailed,
    OrderPurpose,
    OrderSent,
    ProtectionChanged,
)
from perps_agent.risk import AutonomyLevel, Direction, PositionRisk
from perps_agent.venue import OrderStatus, Side


class LedgerError(Exception):
    """An event that contradicts the ledger's state (a bug, or a missed event)."""


@dataclass(slots=True)
class LedgerOrder:
    sent: OrderSent
    status: OrderStatus = OrderStatus.ACCEPTED
    filled_quantity: Decimal = Decimal(0)
    resting_quantity: Decimal = Decimal(0)
    failed: bool = False
    # Whether this entry or add counts toward the day's entry caps.
    counted: bool = False

    @property
    def client_order_id(self) -> str:
        return self.sent.client_order_id

    @property
    def is_live(self) -> bool:
        return not self.failed and self.status.is_live


@dataclass(slots=True)
class LedgerPosition:
    """A position from first fill to flat. ``quantity`` is unsigned."""

    position_id: str
    symbol: str
    instrument_id: int
    direction: Direction
    opened_at: datetime
    level: AutonomyLevel | None
    playbook: str | None
    # Risk budget approved for the whole position (adds stay within it, ADR-0009).
    approved_risk: Decimal
    # The stop the risk engine approved; ``stop_price`` is what the venue confirmed.
    planned_stop: Decimal | None
    planned_target: Decimal | None
    time_stop: datetime | None
    quantity: Decimal = Decimal(0)
    average_entry: Decimal = Decimal(0)
    max_quantity: Decimal = Decimal(0)
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    realized_pnl: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    fills: int = 0
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def net_pnl(self) -> Decimal:
        """Realized PnL after fees and funding."""
        return self.realized_pnl - self.fees + self.funding

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        return self.quantity * (mark - self.average_entry) * self.direction.sign

    def risk(self, mark: Decimal) -> PositionRisk:
        stop = self.stop_price if self.stop_price is not None else self.planned_stop
        return PositionRisk(
            symbol=self.symbol,
            direction=self.direction,
            quantity=self.quantity,
            average_entry=self.average_entry,
            stop_price=stop if stop is not None else self.average_entry,
            approved_risk=self.approved_risk,
            unrealized_pnl=self.unrealized_pnl(mark),
        )


class Ledger:
    """The account's books. ``sink`` receives each event as it is recorded."""

    def __init__(
        self,
        starting_collateral: Decimal,
        *,
        sink: Callable[[LedgerEvent], None] | None = None,
    ) -> None:
        self.starting_collateral = starting_collateral
        self._sink = sink
        self._events: list[LedgerEvent] = []
        self._orders: dict[str, LedgerOrder] = {}
        self._positions: dict[str, LedgerPosition] = {}
        self._open: dict[str, str] = {}  # symbol → open position id
        self._day_start_equity = starting_collateral
        self._entries_today = 0
        self._l2_entries_today = 0

    @classmethod
    def from_events(cls, starting_collateral: Decimal, events: Iterable[LedgerEvent]) -> "Ledger":
        ledger = cls(starting_collateral)
        for event in events:
            ledger._apply(event)
            ledger._events.append(event)
        return ledger

    # --- Recording ---

    def record(self, event: LedgerEvent) -> LedgerEvent:
        """Assign the next sequence number, apply the event and pass it to the sink."""
        event = event.model_copy(update={"sequence": len(self._events) + 1})
        self._apply(event)
        self._events.append(event)
        if self._sink is not None:
            self._sink(event)
        return event

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    # --- Queries ---

    @property
    def collateral(self) -> Decimal:
        return self.starting_collateral + sum(
            (p.realized_pnl - p.fees + p.funding for p in self._positions.values()), Decimal(0)
        )

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Collateral plus unrealized PnL. A position without a mark is valued at entry."""
        return self.collateral + sum(
            (p.unrealized_pnl(marks.get(p.symbol, p.average_entry)) for p in self.open_positions),
            Decimal(0),
        )

    @property
    def day_start_equity(self) -> Decimal:
        return self._day_start_equity

    @property
    def entries_today(self) -> int:
        return self._entries_today

    @property
    def l2_entries_today(self) -> int:
        return self._l2_entries_today

    @property
    def positions(self) -> tuple[LedgerPosition, ...]:
        return tuple(self._positions.values())

    @property
    def open_positions(self) -> tuple[LedgerPosition, ...]:
        return tuple(self._positions[pid] for pid in self._open.values())

    def open_position(self, symbol: str) -> LedgerPosition | None:
        pid = self._open.get(symbol)
        return self._positions[pid] if pid is not None else None

    def position(self, position_id: str) -> LedgerPosition | None:
        return self._positions.get(position_id)

    def order(self, client_order_id: str) -> LedgerOrder | None:
        return self._orders.get(client_order_id)

    def live_orders(
        self, symbol: str | None = None, purposes: Iterable[OrderPurpose] | None = None
    ) -> tuple[LedgerOrder, ...]:
        wanted = set(purposes) if purposes is not None else None
        return tuple(
            o
            for o in self._orders.values()
            if o.is_live
            and (symbol is None or o.sent.symbol == symbol)
            and (wanted is None or o.sent.purpose in wanted)
        )

    # --- Applying events ---

    def _apply(self, event: LedgerEvent) -> None:
        match event:
            case DayStarted():
                self._day_start_equity = event.equity
                self._entries_today = 0
                self._l2_entries_today = 0
                for order in self._orders.values():
                    order.counted = False
            case OrderSent():
                if event.client_order_id in self._orders:
                    raise LedgerError(f"order {event.client_order_id} recorded twice")
                order = LedgerOrder(sent=event)
                self._orders[event.client_order_id] = order
                if event.purpose.opens:
                    self._count_entry(order, 1)
            case OrderFailed():
                order = self._known_order(event.client_order_id)
                order.failed = True
                self._release_unfilled(order)
            case OrderChanged():
                order = self._known_order(event.client_order_id)
                order.status = event.status
                order.filled_quantity = event.filled_quantity
                order.resting_quantity = event.resting_quantity
                self._release_unfilled(order)
            case FillBooked():
                self._apply_fill(event)
            case FundingBooked():
                self._known_position(event.position_id).funding += event.amount
            case ProtectionChanged():
                position = self._known_position(event.position_id)
                position.stop_price = event.stop_price
                position.target_price = event.target_price

    def _count_entry(self, order: LedgerOrder, step: int) -> None:
        order.counted = step > 0
        self._entries_today += step
        if order.sent.level is AutonomyLevel.L2:
            self._l2_entries_today += step

    def _release_unfilled(self, order: LedgerOrder) -> None:
        """An entry or add that ended without any fill gives its slot back."""
        if order.counted and not order.is_live and order.filled_quantity == 0:
            self._count_entry(order, -1)

    def _apply_fill(self, fill: FillBooked) -> None:
        order = self._known_order(fill.client_order_id)
        if order.sent.purpose.opens and not order.counted:
            # A late fill on an entry already given back: it counts after all.
            self._count_entry(order, 1)
        position = self._positions.get(fill.position_id)
        if position is None:
            if not order.sent.purpose.opens:
                raise LedgerError(f"{order.sent.purpose} fill for unknown position")
            position = self._open_position(order.sent, fill)
        elif not position.is_open:
            raise LedgerError(f"fill for closed position {fill.position_id}")
        same = fill.side is position.direction.side
        if same:
            size = position.quantity + fill.quantity
            position.average_entry = (
                position.quantity * position.average_entry + fill.quantity * fill.price
            ) / size
            position.quantity = size
            position.max_quantity = max(position.max_quantity, size)
            if order.sent.purpose is OrderPurpose.ADD and order.sent.stop_price is not None:
                planned = position.planned_stop
                tighter = planned is None or (
                    (order.sent.stop_price - planned) * position.direction.sign > 0
                )
                if tighter:
                    position.planned_stop = order.sent.stop_price
            if order.sent.purpose is OrderPurpose.ADD and order.sent.time_stop is not None:
                # An add may give the position more time, never less.
                if position.time_stop is not None and order.sent.time_stop > position.time_stop:
                    position.time_stop = order.sent.time_stop
        else:
            if fill.quantity > position.quantity:
                raise LedgerError(
                    f"{fill.quantity} fill would flip position {fill.position_id} "
                    f"({position.quantity})"
                )
            position.realized_pnl += (
                fill.quantity * (fill.price - position.average_entry) * position.direction.sign
            )
            position.quantity -= fill.quantity
        position.fees += fill.fee
        position.fills += 1
        if position.quantity == 0:
            position.closed_at = fill.at
            del self._open[position.symbol]

    def _open_position(self, sent: OrderSent, fill: FillBooked) -> LedgerPosition:
        if sent.purpose is not OrderPurpose.ENTRY:
            raise LedgerError(f"an {sent.purpose} order cannot open a position")
        if sent.symbol in self._open:
            raise LedgerError(f"{sent.symbol} already has an open position")
        position = LedgerPosition(
            position_id=fill.position_id,
            symbol=sent.symbol,
            instrument_id=sent.instrument_id,
            direction=Direction.LONG if sent.side is Side.BUY else Direction.SHORT,
            opened_at=fill.at,
            level=sent.level,
            playbook=sent.playbook,
            approved_risk=sent.approved_risk or Decimal(0),
            planned_stop=sent.stop_price,
            planned_target=sent.target_price,
            time_stop=sent.time_stop,
        )
        self._positions[position.position_id] = position
        self._open[position.symbol] = position.position_id
        return position

    def _known_order(self, client_order_id: str) -> LedgerOrder:
        try:
            return self._orders[client_order_id]
        except KeyError:
            raise LedgerError(f"unknown order {client_order_id}") from None

    def _known_position(self, position_id: str) -> LedgerPosition:
        try:
            return self._positions[position_id]
        except KeyError:
            raise LedgerError(f"unknown position {position_id}") from None
