"""Venue domain models.

These are the only venue types the rest of the engine sees (ADR-0001). Prices,
quantities and money are ``Decimal``; timestamps are timezone-aware UTC
datetimes with millisecond precision, as the venue reports them.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from perps_agent.clock import to_utc


def _utc(value: datetime) -> datetime:
    return to_utc(value)


UtcDatetime = Annotated[datetime, AfterValidator(_utc)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
InstrumentId = Annotated[int, Field(ge=0)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Side(StrEnum):
    """Order side. For public trades it is the aggressor's side."""

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class InstrumentCategory(StrEnum):
    EQUITY = "equity"
    INDEX = "index"
    COMMODITY = "commodity"
    CRYPTO = "crypto"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "InstrumentCategory":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


# --- Reference data -------------------------------------------------------------------


class RiskTier(_Model):
    """From ``notional_floor`` (position notional, pUSD) up, leverage is capped."""

    notional_floor: NonNegativeDecimal
    max_leverage: Annotated[int, Field(ge=1)]


class Instrument(_Model):
    """A tradable perpetual and its venue limits, as read from the venue (never hard-coded)."""

    instrument_id: InstrumentId
    symbol: str
    category: InstrumentCategory
    base_asset: str
    quote_asset: str
    price_decimals: Annotated[int, Field(ge=0)]
    quantity_decimals: Annotated[int, Field(ge=0)]
    # Orders must be priced within mark × (1 ± price_band).
    price_band: PositiveDecimal
    min_notional: NonNegativeDecimal
    max_market_notional: PositiveDecimal
    max_limit_notional: PositiveDecimal
    max_leverage: Annotated[int, Field(ge=1)]
    risk_tiers: tuple[RiskTier, ...]
    max_order_count: Annotated[int, Field(ge=1)]
    liquidation_fee_rate: NonNegativeDecimal
    funding_interval: str
    isolated_only: bool
    close_only: bool
    settled: bool

    @property
    def tick_size(self) -> Decimal:
        return Decimal(1).scaleb(-self.price_decimals)

    @property
    def lot_size(self) -> Decimal:
        return Decimal(1).scaleb(-self.quantity_decimals)

    @property
    def maintenance_margin_rate(self) -> Decimal:
        """Flat MM rate, ``0.5 / max_leverage`` (venue research §5, §9)."""
        return Decimal("0.5") / self.max_leverage

    @property
    def accepts_new_exposure(self) -> bool:
        return not (self.close_only or self.settled)

    def max_leverage_for(self, notional: Decimal) -> int:
        """Venue leverage cap for a position of ``notional`` pUSD, from the risk tiers."""
        cap = self.max_leverage
        for tier in self.risk_tiers:
            if notional >= tier.notional_floor:
                cap = min(cap, tier.max_leverage)
        return cap


class ExchangeStatus(_Model):
    engine_version: str
    cancel_only: bool


# --- Market data ----------------------------------------------------------------------


class BookLevel(_Model):
    price: PositiveDecimal
    quantity: NonNegativeDecimal


class OrderBook(_Model):
    """Book snapshot. ``bids`` best (highest) first, ``asks`` best (lowest) first.

    ``sequence`` is the venue sequence: the REST book sequence, or the channel
    ``sq`` for a WebSocket frame. WebSocket book frames are top-N snapshots.
    """

    instrument_id: InstrumentId
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    timestamp: UtcDatetime
    sequence: int

    @model_validator(mode="after")
    def _sorted(self) -> Self:
        bid_prices = [level.price for level in self.bids]
        ask_prices = [level.price for level in self.asks]
        if bid_prices != sorted(bid_prices, reverse=True):
            raise ValueError("bids must be sorted best (highest) first")
        if ask_prices != sorted(ask_prices):
            raise ValueError("asks must be sorted best (lowest) first")
        return self

    @property
    def best_bid(self) -> BookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> BookLevel | None:
        return self.asks[0] if self.asks else None


class Bbo(_Model):
    instrument_id: InstrumentId
    bid_price: Decimal
    bid_quantity: NonNegativeDecimal
    ask_price: Decimal
    ask_quantity: NonNegativeDecimal
    timestamp: UtcDatetime
    sequence: int

    @property
    def mid(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2

    @property
    def spread_bp(self) -> Decimal:
        return (self.ask_price - self.bid_price) / self.mid * 10_000


class Trade(_Model):
    trade_id: int
    instrument_id: InstrumentId
    aggressor: Side
    price: PositiveDecimal
    quantity: PositiveDecimal
    timestamp: UtcDatetime
    # True when the trade closes a position at instrument settlement.
    settlement: bool = False


class Ticker(_Model):
    """Prices and funding state. ``funding_rate`` is per funding interval (1h)."""

    instrument_id: InstrumentId
    mark_price: Decimal
    index_price: Decimal
    last_price: Decimal
    mid_price: Decimal
    open_interest: NonNegativeDecimal
    funding_rate: Decimal
    next_funding: UtcDatetime
    timestamp: UtcDatetime
    # Only on REST tickers (from /statistics); absent on stream updates.
    open_price_24h: Decimal | None = None
    volume_24h: NonNegativeDecimal | None = None


class FundingRate(_Model):
    instrument_id: InstrumentId
    rate: Decimal
    timestamp: UtcDatetime


class Candle(_Model):
    instrument_id: InstrumentId
    interval: str
    open_time: UtcDatetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: NonNegativeDecimal
    trade_count: Annotated[int, Field(ge=0)]


class MarkPoint(_Model):
    instrument_id: InstrumentId
    timestamp: UtcDatetime
    mark_price: Decimal


# --- Stream events --------------------------------------------------------------------


class StreamChannel(StrEnum):
    BBO = "bbo"
    BOOK = "book"
    TRADES = "trades"
    TICKER = "ticker"


class _StreamEvent(_Model):
    """Common envelope: the venue channel name and its per-channel sequence ``sq``.

    Gap detection on ``sequence`` is the consumer's job (market data feed, WP3).
    """

    channel: str
    sequence: int
    received_at: UtcDatetime


class BboEvent(_StreamEvent):
    kind: StreamChannel = StreamChannel.BBO
    bbo: Bbo


class BookEvent(_StreamEvent):
    kind: StreamChannel = StreamChannel.BOOK
    book: OrderBook


class TradesEvent(_StreamEvent):
    kind: StreamChannel = StreamChannel.TRADES
    trades: tuple[Trade, ...]


class TickerEvent(_StreamEvent):
    kind: StreamChannel = StreamChannel.TICKER
    ticker: Ticker


MarketEvent = BboEvent | BookEvent | TradesEvent | TickerEvent


# --- Orders (used by the fake exchange and the simulator; live gateway comes later) ----


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(StrEnum):
    """Venue order statuses (venue research §10). Unknown values map to ``UNKNOWN``."""

    ACCEPTED = "accepted"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    AUTO_CANCELLED = "auto_cancelled"
    POST_ONLY_REJECTED = "post_only_rejected"
    FOK_UNFILLED = "fok_unfilled"
    IOC_NO_FILL = "ioc_no_fill"
    IOC_EXPIRED = "ioc_expired"
    STP_CANCELLED = "stp_cancelled"
    ZERO_QUANTITY = "zero_quantity"
    DUPLICATE_ORDER = "duplicate_order"
    REDUCE_ONLY_INVALID = "reduce_only_invalid"
    REDUCE_ONLY_EXPIRED = "reduce_only_expired"
    ORDER_EXPIRED = "order_expired"
    SWEEP_CAP_EXCEEDED = "sweep_cap_exceeded"
    RESTING_ORDER_LIMIT_EXCEEDED = "resting_order_limit_exceeded"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    PRICE_OUTSIDE_BOUNDS = "price_outside_bounds"
    INSTRUMENT_DISABLED = "instrument_disabled"
    INSTRUMENT_CLOSE_ONLY = "instrument_close_only"
    INSTRUMENT_SETTLED = "instrument_settled"
    INSUFFICIENT_MARGIN_AT_FILL = "insufficient_margin_at_fill"
    MARK_PRICE_UNAVAILABLE = "mark_price_unavailable"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str) -> "OrderStatus":
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def is_live(self) -> bool:
        return self in _LIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        # UNKNOWN is neither: the order must be reconciled before it is trusted either way.
        return self not in _LIVE_STATUSES and self is not OrderStatus.UNKNOWN


_LIVE_STATUSES = frozenset({OrderStatus.ACCEPTED, OrderStatus.OPEN, OrderStatus.PARTIAL})


class OrderRequest(_Model):
    """An order to send. ``price`` is ``None`` for a market-style IOC/FOK order."""

    client_order_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    instrument_id: InstrumentId
    side: Side
    quantity: PositiveDecimal
    price: PositiveDecimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.post_only and self.time_in_force is not TimeInForce.GTC:
            raise ValueError("post_only is only valid with GTC")
        if self.price is None and self.time_in_force is TimeInForce.GTC:
            raise ValueError("a GTC order needs a limit price")
        return self

    @property
    def is_market(self) -> bool:
        return self.price is None


class OrderState(_Model):
    order_id: int
    request: OrderRequest
    status: OrderStatus
    filled_quantity: NonNegativeDecimal
    resting_quantity: NonNegativeDecimal
    created_at: UtcDatetime
    updated_at: UtcDatetime


class Fill(_Model):
    trade_id: int
    order_id: int
    client_order_id: str | None
    instrument_id: InstrumentId
    side: Side
    price: PositiveDecimal
    quantity: PositiveDecimal
    fee: Decimal
    is_taker: bool
    timestamp: UtcDatetime
    liquidation: bool = False


class Position(_Model):
    """Signed position: ``quantity`` > 0 long, < 0 short."""

    instrument_id: InstrumentId
    quantity: Decimal
    entry_price: PositiveDecimal
    leverage: Decimal
    isolated: bool


class AccountSummary(_Model):
    equity: Decimal
    available_margin: Decimal
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
