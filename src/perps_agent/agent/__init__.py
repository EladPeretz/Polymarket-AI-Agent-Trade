"""Agent bridge (overview §3.5): the tools, prompts and transport between Claude and the engine."""

from perps_agent.agent.bridge import AgentBridge, AgentEvent, MarketView, ToolError
from perps_agent.agent.briefing import (
    SessionActivity,
    build_briefing,
    recent_activity,
    system_prompt,
)
from perps_agent.agent.schemas import TradeProposal
from perps_agent.agent.transport import BridgeServer, TransportError, request

__all__ = [
    "AgentBridge",
    "AgentEvent",
    "BridgeServer",
    "MarketView",
    "SessionActivity",
    "ToolError",
    "TradeProposal",
    "TransportError",
    "build_briefing",
    "recent_activity",
    "request",
    "system_prompt",
]
