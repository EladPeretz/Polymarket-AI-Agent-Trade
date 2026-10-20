"""Domain-model builders for venue unit tests."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from perps_agent.venue.models import (
    Instrument,
    InstrumentCategory,
    OrderRequest,
    RiskTier,
    Side,
    Ticker,
    TimeInForce,
)

T0 = datetime(2026, 10, 20, 13, 30, tzinfo=UTC)


def instrument(**overrides: Any) -> Instrument:
    """NVDA-USD-like parameters (as recorded 2026-10-20)."""
    fields: dict[str, Any] = {
        "instrument_id": 19,
        "symbol": "NVDA-USD",
        "category": InstrumentCategory.EQUITY,
        "base_asset": "NVDA",
        "quote_asset": "pUSD",
        "price_decimals": 2,
        "quantity_decimals": 4,
        "price_band": Decimal("0.10"),
        "min_notional": Decimal(10),
        "max_market_notional": Decimal(500_000),
        "max_limit_notional": Decimal(2_500_000),
        "max_leverage": 10,
        "risk_tiers": (
            RiskTier(notional_floor=Decimal(0), max_leverage=10),
            RiskTier(notional_floor=Decimal(500_000), max_leverage=5),
            RiskTier(notional_floor=Decimal(1_000_000), max_leverage=3),
        ),
        "max_order_count": 200,
        "liquidation_fee_rate": Decimal("0.005"),
        "funding_interval": "1h",
        "isolated_only": False,
        "close_only": False,
        "settled": False,
    }
    fields.update(overrides)
    return Instrument(**fields)


def ticker(mark: str = "200", instrument_id: int = 19) -> Ticker:
    price = Decimal(mark)
    return Ticker(
        instrument_id=instrument_id,
        mark_price=price,
        index_price=price,
        last_price=price,
        mid_price=price,
        open_interest=Decimal(1000),
        funding_rate=Decimal("0.00000625"),
        next_funding=T0,
        timestamp=T0,
    )


def order(
    quantity: str = "1",
    price: str | None = "200",
    side: Side = Side.BUY,
    coid: str = "0" * 32,
    **overrides: Any,
) -> OrderRequest:
    fields: dict[str, Any] = {
        "client_order_id": coid,
        "instrument_id": 19,
        "side": side,
        "quantity": Decimal(quantity),
        "price": None if price is None else Decimal(price),
        "time_in_force": TimeInForce.GTC if price is not None else TimeInForce.IOC,
    }
    fields.update(overrides)
    return OrderRequest(**fields)
