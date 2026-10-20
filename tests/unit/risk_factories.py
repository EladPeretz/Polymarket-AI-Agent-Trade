"""Builders for risk-engine unit tests. Defaults mirror config/default.yaml."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from perps_agent.calendar import TradingCalendar
from perps_agent.config.models import RiskConfig, SessionConfig, SymbolConfig
from perps_agent.market import MarketSnapshot
from perps_agent.risk import (
    AutonomyLevel,
    Direction,
    PositionRisk,
    RiskEngine,
    RiskState,
    TradeIntent,
)
from perps_agent.venue import BookLevel, StreamChannel
from tests.unit.venue_factories import T0, ticker

D = Decimal
# 10:00 New York on a trading day (Tuesday 2026-10-20), inside the entry window.
NOW = T0 + timedelta(minutes=30)

RISK = RiskConfig(
    per_trade_risk_fraction=D("0.007"),
    daily_loss_limit_fraction=D("0.03"),
    max_leverage=D(10),
    max_position_notional=D(5000),
    max_concurrent_positions=3,
    max_trades_per_day=12,
    max_l2_trades_per_day=4,
    l2_risk_fraction=D("0.5"),
    min_stop_distance_bp=D(20),
    max_spread_bp=D(6),
    min_top_of_book_notional=D(2000),
    liquidation_buffer_fraction=D("0.5"),
    max_snapshot_age_ms=2000,
)


class OrderIds:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.n:032x}"


def engine(risk: RiskConfig = RISK, symbols: tuple[SymbolConfig, ...] | None = None) -> RiskEngine:
    return RiskEngine(
        risk,
        symbols or (SymbolConfig(symbol="NVDA-USD"),),
        TradingCalendar(SessionConfig()),
        new_order_id=OrderIds(),
    )


def snapshot(
    *,
    bid: str = "199.98",
    ask: str = "200.02",
    mark: str = "200",
    quantity: str = "20",
    taken_at: datetime = NOW,
    stale: bool = False,
    asks: tuple[tuple[str, str], ...] | None = None,
    bids: tuple[tuple[str, str], ...] | None = None,
) -> MarketSnapshot:
    def levels(pairs: tuple[tuple[str, str], ...]) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in pairs)

    return MarketSnapshot(
        symbol="NVDA-USD",
        instrument_id=19,
        taken_at=taken_at,
        bids=levels(bids or ((bid, quantity), (str(D(bid) - 1), "50"))),
        asks=levels(asks or ((ask, quantity), (str(D(ask) + 1), "50"))),
        book_time=taken_at,
        bbo=None,
        ticker=ticker(mark),
        recent_trades=(),
        channel_age_ms={channel: 0 for channel in StreamChannel},
        stale=stale,
        stale_reasons=("book: no stream update for 3.0s",) if stale else (),
    )


def state(
    equity: str = "500",
    *,
    available: str | None = None,
    day_start: str | None = None,
    positions: tuple[PositionRisk, ...] = (),
    **overrides: Any,
) -> RiskState:
    return RiskState(
        equity=D(equity),
        available_margin=D(available if available is not None else equity),
        day_start_equity=D(day_start if day_start is not None else equity),
        positions={p.symbol: p for p in positions},
        **overrides,
    )


def intent(
    direction: Direction = Direction.LONG,
    *,
    entry: str | None = "200",
    stop: str = "199",
    level: AutonomyLevel = AutonomyLevel.L1,
    size: str = "1",
    **overrides: Any,
) -> TradeIntent:
    fields: dict[str, Any] = {
        "symbol": "NVDA-USD",
        "direction": direction,
        "level": level,
        "playbook": None if level is AutonomyLevel.L2 else "orb@1",
        "entry_price": None if entry is None else D(entry),
        "size": D(size),
        "stop_price": D(stop),
        "thesis": "opening range breakout on volume",
        "confidence": D("0.6"),
    }
    fields.update(overrides)
    return TradeIntent(**fields)


def position(
    direction: Direction = Direction.LONG,
    *,
    quantity: str = "2",
    entry: str = "198",
    stop: str = "197",
    approved_risk: str = "3.5",
    pnl: str = "4",
) -> PositionRisk:
    return PositionRisk(
        symbol="NVDA-USD",
        direction=direction,
        quantity=D(quantity),
        average_entry=D(entry),
        stop_price=D(stop),
        approved_risk=D(approved_risk),
        unrealized_pnl=D(pnl),
    )
