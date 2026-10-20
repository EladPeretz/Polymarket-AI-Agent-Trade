"""What execution needs from a venue: orders, leverage, position TP/SL and account reads.

Paper mode uses :class:`SimBackend` over the simulator. The live gateway will
implement the same protocol on the approved VPS only (ADR-0004).

Execution reports use the simulator's event vocabulary (``sim.SimEvent``:
order updates, fills, funding, TP/SL triggers, liquidations); the live
gateway will map venue updates onto it.
"""

from decimal import Decimal
from typing import Protocol

from perps_agent.sim import PaperSimulator, PositionProtection
from perps_agent.venue import OrderRequest, OrderState
from perps_agent.venue.models import AccountSummary, Position


class ExecutionBackend(Protocol):
    async def place_order(self, request: OrderRequest) -> OrderState: ...

    async def cancel_order(self, client_order_id: str) -> OrderState: ...

    async def set_leverage(self, instrument_id: int, leverage: Decimal) -> None: ...

    async def set_protection(
        self, instrument_id: int, stop_loss: Decimal | None, take_profit: Decimal | None
    ) -> None:
        """Replace the position TP/SL (mark-triggered, closes the whole position)."""
        ...

    async def fetch_account(self) -> AccountSummary: ...

    async def fetch_positions(self) -> tuple[Position, ...]: ...


class SimBackend:
    """The paper simulator as an execution backend. Nothing is signed or sent."""

    def __init__(self, simulator: PaperSimulator) -> None:
        self.simulator = simulator

    async def place_order(self, request: OrderRequest) -> OrderState:
        return self.simulator.submit(request)

    async def cancel_order(self, client_order_id: str) -> OrderState:
        return self.simulator.cancel(client_order_id)

    async def set_leverage(self, instrument_id: int, leverage: Decimal) -> None:
        self.simulator.set_leverage(instrument_id, leverage)

    async def set_protection(
        self, instrument_id: int, stop_loss: Decimal | None, take_profit: Decimal | None
    ) -> None:
        self.simulator.set_protection(
            instrument_id, PositionProtection(stop_loss=stop_loss, take_profit=take_profit)
        )

    async def fetch_account(self) -> AccountSummary:
        return self.simulator.account_summary()

    async def fetch_positions(self) -> tuple[Position, ...]:
        return await self.simulator.fetch_positions()
