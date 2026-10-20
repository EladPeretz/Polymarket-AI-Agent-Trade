"""The portfolio view the risk engine decides on. The ledger (WP7) builds it."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from perps_agent.risk.intent import Direction


@dataclass(frozen=True, slots=True)
class PositionRisk:
    """An open position as risk sees it. ``quantity`` is unsigned."""

    symbol: str
    direction: Direction
    quantity: Decimal
    average_entry: Decimal
    stop_price: Decimal
    # The most the position may lose to its stop, fixed when it was approved (ADR-0009).
    approved_risk: Decimal
    unrealized_pnl: Decimal

    @property
    def risk_to_stop(self) -> Decimal:
        """What the whole position loses if the stop fills at its price (0 once locked in)."""
        loss = self.quantity * (self.average_entry - self.stop_price) * self.direction.sign
        return max(Decimal(0), loss)


@dataclass(frozen=True, slots=True)
class RiskState:
    """Account and day counters at decision time (pUSD)."""

    equity: Decimal
    available_margin: Decimal
    # Equity at the start of the trading day: the base for the daily loss limit.
    day_start_equity: Decimal
    positions: Mapping[str, PositionRisk] = field(default_factory=dict)
    # Symbols with an entry order still working (they count as positions).
    pending_entries: frozenset[str] = frozenset()
    # Entries (new positions and adds) sent today, and how many of them were L2.
    trades_today: int = 0
    l2_trades_today: int = 0

    @property
    def day_pnl(self) -> Decimal:
        """Realized plus unrealized PnL since the day started, after fees and funding."""
        return self.equity - self.day_start_equity

    @property
    def exposed_symbols(self) -> frozenset[str]:
        return frozenset(self.positions) | self.pending_entries
