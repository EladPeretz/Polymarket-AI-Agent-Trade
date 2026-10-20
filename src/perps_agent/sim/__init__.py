"""Paper/backtest fill simulator (overview §3.9, ADR-0005).

Built on the venue interfaces and domain models only (ADR-0001); nothing here
imports the SDK, signs or sends anything.
"""

from perps_agent.sim.account import SimAccount, SimPosition
from perps_agent.sim.replay import ReplayClock, replay
from perps_agent.sim.simulator import (
    FillEvent,
    FundingPayment,
    Liquidation,
    OrderUpdate,
    PaperSimulator,
    PositionProtection,
    ProtectionTriggered,
    SimEvent,
)

__all__ = [
    "FillEvent",
    "FundingPayment",
    "Liquidation",
    "OrderUpdate",
    "PaperSimulator",
    "PositionProtection",
    "ProtectionTriggered",
    "ReplayClock",
    "SimAccount",
    "SimEvent",
    "SimPosition",
    "replay",
]
