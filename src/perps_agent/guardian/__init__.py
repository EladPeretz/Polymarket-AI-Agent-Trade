"""Guardian (overview §3.10): window close-out, kill switch, liveness and incidents."""

from perps_agent.guardian.guardian import (
    HOLD_AGENT,
    HOLD_KILL_SWITCH,
    HOLD_MISMATCH,
    KILL_SWITCH_REASON,
    WINDOW_CLOSE_REASON,
    AgentActivity,
    Guardian,
)
from perps_agent.guardian.incidents import (
    INCIDENTS_FILE,
    Incident,
    IncidentKind,
    IncidentLog,
    JsonlIncidentSink,
    Severity,
    append_incident,
    load_incidents,
    log_alert,
)

__all__ = [
    "HOLD_AGENT",
    "HOLD_KILL_SWITCH",
    "HOLD_MISMATCH",
    "INCIDENTS_FILE",
    "KILL_SWITCH_REASON",
    "WINDOW_CLOSE_REASON",
    "AgentActivity",
    "Guardian",
    "Incident",
    "IncidentKind",
    "IncidentLog",
    "JsonlIncidentSink",
    "Severity",
    "append_incident",
    "load_incidents",
    "log_alert",
]
