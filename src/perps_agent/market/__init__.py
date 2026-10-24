"""Market data: the live feed, the state store and its snapshots, and the recorder.

Built on the venue interfaces only (ADR-0001); nothing here imports the SDK.
"""

from perps_agent.market.feed import MarketFeed
from perps_agent.market.recorder import (
    FeedStatus,
    Recorder,
    RestRecord,
    StatusRecord,
    StreamRecord,
    read_recording,
    recording_path,
)
from perps_agent.market.state import REQUIRED_CHANNELS, MarketSnapshot, MarketStateStore

__all__ = [
    "REQUIRED_CHANNELS",
    "FeedStatus",
    "MarketFeed",
    "MarketSnapshot",
    "MarketStateStore",
    "Recorder",
    "RestRecord",
    "StatusRecord",
    "StreamRecord",
    "read_recording",
    "recording_path",
]
