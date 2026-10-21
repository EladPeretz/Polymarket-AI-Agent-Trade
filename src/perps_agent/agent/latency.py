"""Agent latency (overview §3.12): how fast the agent picks up and answers events.

For every event ``next_event`` delivers, the bridge records when the event was
raised, when it reached the agent, and when the agent made its next tool call
(any tool, ``next_event`` included: deciding to wait is an answer too). The
daily report summarizes them. Stored as ``results/<date>/agent_events.jsonl``.
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from perps_agent.journal import TriggerKind
from perps_agent.venue.models import UtcDatetime

AGENT_EVENTS_FILE = "agent_events.jsonl"


class EventLatency(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    kind: TriggerKind
    key: str
    symbol: str | None = None
    raised_at: UtcDatetime
    delivered_at: UtcDatetime
    # ``None``: the session ended before the agent's next call.
    answered_at: UtcDatetime | None = None
    answer_tool: str | None = None

    @property
    def pickup_seconds(self) -> float:
        return (self.delivered_at - self.raised_at).total_seconds()

    @property
    def answer_seconds(self) -> float | None:
        if self.answered_at is None:
            return None
        return (self.answered_at - self.delivered_at).total_seconds()


_ADAPTER: TypeAdapter[EventLatency] = TypeAdapter(EventLatency)


class JsonlLatencySink:
    """Pass as ``AgentBridge(latency_sink=...)``: appends and flushes each record."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def __call__(self, record: EventLatency) -> None:
        self._file.write(_ADAPTER.dump_json(record).decode() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def load_latencies(path: Path) -> list[EventLatency]:
    with path.open(encoding="utf-8") as lines:
        return [_ADAPTER.validate_json(line) for line in lines if line.strip()]
