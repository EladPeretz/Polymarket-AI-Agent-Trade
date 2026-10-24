"""Ledger events (overview §3.11): every order, fill, funding payment and fee.

The ledger is event-sourced: positions and PnL are derived from these records
only, so the event log alone rebuilds the account. ``sequence`` is assigned
by the ledger when an event is recorded.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from perps_agent.risk import AutonomyLevel
from perps_agent.venue import OrderStatus, Side, TimeInForce
from perps_agent.venue.models import UtcDatetime


class OrderPurpose(StrEnum):
    ENTRY = "entry"
    ADD = "add"
    EXIT = "exit"
    REDUCE = "reduce"
    # Sent by the venue (or simulator) itself: a position TP/SL trigger, a liquidation.
    PROTECTION = "protection"
    LIQUIDATION = "liquidation"

    @property
    def opens(self) -> bool:
        return self in (OrderPurpose.ENTRY, OrderPurpose.ADD)


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = 0
    at: UtcDatetime


class DayStarted(_Event):
    """A trading day began: day counters reset, and the daily loss limit is based here."""

    type: Literal["day_started"] = "day_started"
    day: date
    equity: Decimal


class OrderSent(_Event):
    type: Literal["order_sent"] = "order_sent"
    client_order_id: str
    symbol: str
    instrument_id: int
    position_id: str
    purpose: OrderPurpose
    side: Side
    quantity: Decimal
    price: Decimal | None
    time_in_force: TimeInForce
    post_only: bool = False
    reduce_only: bool = False
    reason: str | None = None
    # Entries and adds only: what the risk engine approved.
    level: AutonomyLevel | None = None
    playbook: str | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    time_stop: UtcDatetime | None = None
    approved_risk: Decimal | None = None
    leverage: int | None = None
    # Limit entries and adds: cancelled if still working this long after being sent.
    timeout_seconds: int | None = None


class OrderFailed(_Event):
    """The venue refused the request outright, so the order never existed there."""

    type: Literal["order_failed"] = "order_failed"
    client_order_id: str
    error: str


class OrderChanged(_Event):
    type: Literal["order_changed"] = "order_changed"
    client_order_id: str
    status: OrderStatus
    filled_quantity: Decimal
    resting_quantity: Decimal


class FillBooked(_Event):
    type: Literal["fill"] = "fill"
    client_order_id: str
    position_id: str
    symbol: str
    trade_id: int
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    is_taker: bool
    liquidation: bool = False


class FundingBooked(_Event):
    type: Literal["funding"] = "funding"
    position_id: str
    symbol: str
    rate: Decimal
    mark_price: Decimal
    # Received; negative when paid.
    amount: Decimal


class ProtectionChanged(_Event):
    """The position TP/SL now confirmed on the venue (``None``: not set)."""

    type: Literal["protection"] = "protection"
    position_id: str
    symbol: str
    stop_price: Decimal | None
    target_price: Decimal | None
    reason: str


LedgerEvent = Annotated[
    DayStarted
    | OrderSent
    | OrderFailed
    | OrderChanged
    | FillBooked
    | FundingBooked
    | ProtectionChanged,
    Field(discriminator="type"),
]
