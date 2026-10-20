"""Journal entries (overview §3.11, ADR-0010): the append-only record of agent decisions.

One decision is a thread of entries sharing a ``decision_id``:

* ``decision``: what woke the agent, the market it saw, the scenarios it weighed
  and the plan it chose (or that it stood aside);
* ``risk_verdict``: the risk engine's answer to the decision's intent;
* ``order_linked`` / ``action_failed``: the ledger order execution sent for it,
  or why execution refused;
* ``outcome``: the closed position's result, derived from the ledger;
* ``review``: the agent's later judgement, which scenario played out and which
  assumptions held. The learning loop counts these (WP14).

Nothing is edited after it is written; corrections and reviews are new entries.
Scenarios, assumptions and reviews are for the learning loop only and are not
shown in daily reports or alerts (ADR-0010). ``sequence`` is assigned by the
journal when an entry is recorded.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from perps_agent.config.models import Mode
from perps_agent.risk import AutonomyLevel, Direction, RiskVerdict, TradeIntent
from perps_agent.venue.models import UtcDatetime

SCHEMA_VERSION = 1

Probability = Annotated[Decimal, Field(ge=0, le=1)]
# Stable identifiers the learning loop groups by: lowercase words joined by "_", "-" or ".".
Key = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_.@-]*$", max_length=80)]
Text = Annotated[str, Field(min_length=1)]

# Scenario probabilities may be rounded by the agent; beyond this they do not add up.
_PROBABILITY_SLACK = Decimal("0.02")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TriggerKind(StrEnum):
    """What woke the agent (overview §3.4)."""

    PLAYBOOK = "playbook"
    LARGE_MOVE = "large_move"
    POSITION = "position"
    ORDER = "order"
    RISK = "risk"
    CHECK_IN = "check_in"
    SESSION = "session"
    # The agent acted on its own initiative between events.
    AGENT = "agent"


class Action(StrEnum):
    ENTER = "enter"
    ADD = "add"
    REDUCE = "reduce"
    CLOSE = "close"
    ADJUST = "adjust"
    STAND_ASIDE = "stand_aside"
    NOTE = "note"

    @property
    def opens(self) -> bool:
        return self in (Action.ENTER, Action.ADD)


class Trigger(_Model):
    kind: TriggerKind
    detail: Text
    # The playbook whose condition fired, for ``playbook`` triggers.
    playbook: str | None = None


class SnapshotRef(_Model):
    """The market the agent decided on, compactly; ``digest`` identifies the full snapshot."""

    symbol: str
    taken_at: UtcDatetime
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    mark_price: Decimal | None
    mid: Decimal | None
    spread_bp: Decimal | None
    stale: bool
    # Features the decision used (WP9: ranges, VWAP, POC, expected move...), by name.
    features: dict[Key, Decimal] = Field(default_factory=dict)


class Assumption(_Model):
    """A belief a plan depends on. ``key`` is stable across days, so outcomes can be counted."""

    key: Key
    statement: Text


class Scenario(_Model):
    name: Key
    description: Text
    probability: Probability
    # Expected result of the chosen plan in this scenario, in R (multiples of risk).
    expected_r: Decimal | None = None
    assumptions: tuple[Assumption, ...] = ()


class ToolCall(_Model):
    """A tool the agent used while deciding (``simulate``, ``get_snapshot``...)."""

    name: Text
    summary: Text


class _Entry(_Model):
    sequence: int = 0
    at: UtcDatetime


class SessionStarted(_Entry):
    type: Literal["session_started"] = "session_started"
    schema_version: int = SCHEMA_VERSION
    day: date
    mode: Mode
    symbols: tuple[str, ...]


class DecisionMade(_Entry):
    """The agent's reasoning for one event, recorded before anything is sent."""

    type: Literal["decision"] = "decision"
    decision_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    trigger: Trigger
    action: Action
    symbol: str | None = None
    snapshot: SnapshotRef | None = None
    # Tags the lessons notebook files the decision under.
    regime: Key | None = None
    setup: Key | None = None
    thesis: Text
    scenarios: tuple[Scenario, ...] = ()
    # The scenario the plan bets on; ``None`` when standing aside or noting.
    chosen_scenario: Key | None = None
    # Probability-weighted result of the plan, in R.
    expected_r: Decimal | None = None
    intent: TradeIntent | None = None
    # Reduce/close fraction, adjust targets and the like: what was asked, in words.
    request: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.action.opens:
            if self.intent is None:
                raise ValueError(f"an {self.action} decision needs its trade intent")
            if not self.scenarios:
                raise ValueError(f"an {self.action} decision needs the scenarios it weighed")
            if self.chosen_scenario is None:
                raise ValueError(f"an {self.action} decision needs its chosen scenario")
        elif self.intent is not None:
            raise ValueError(f"a {self.action} decision does not carry a trade intent")
        if self.intent is not None and self.symbol not in (None, self.intent.symbol):
            raise ValueError(f"symbol {self.symbol} does not match the intent's")
        if self.snapshot is not None and self.symbol not in (None, self.snapshot.symbol):
            raise ValueError(f"symbol {self.symbol} does not match the snapshot's")
        names = [s.name for s in self.scenarios]
        if len(set(names)) != len(names):
            raise ValueError("scenario names must be unique")
        if self.chosen_scenario is not None and self.chosen_scenario not in names:
            raise ValueError(f"chosen scenario {self.chosen_scenario} is not among the scenarios")
        total = sum((s.probability for s in self.scenarios), Decimal(0))
        if total > 1 + _PROBABILITY_SLACK:
            raise ValueError(f"scenario probabilities add up to {total}, more than 1")
        return self

    @property
    def assumptions(self) -> tuple[Assumption, ...]:
        """Every distinct assumption across the scenarios, in order of appearance."""
        seen: dict[str, Assumption] = {}
        for scenario in self.scenarios:
            for assumption in scenario.assumptions:
                seen.setdefault(assumption.key, assumption)
        return tuple(seen.values())


class RiskVerdictRecorded(_Entry):
    type: Literal["risk_verdict"] = "risk_verdict"
    decision_id: str
    verdict: RiskVerdict


class OrderLinked(_Entry):
    """Execution sent an order for the decision; the ledger holds its fills."""

    type: Literal["order_linked"] = "order_linked"
    decision_id: str
    client_order_id: str
    position_id: str


class ActionFailed(_Entry):
    """The decision was not carried out: execution or the venue refused it."""

    type: Literal["action_failed"] = "action_failed"
    decision_id: str
    error: Text


class TradeOutcome(_Entry):
    """A closed position's result, from the ledger. Money in pUSD, after costs."""

    type: Literal["outcome"] = "outcome"
    decision_id: str
    position_id: str
    symbol: str
    direction: Direction
    level: AutonomyLevel | None
    playbook: str | None
    opened_at: UtcDatetime
    closed_at: UtcDatetime
    max_quantity: Decimal
    average_entry: Decimal
    average_exit: Decimal
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    approved_risk: Decimal
    # ``net_pnl / approved_risk``; ``None`` when no risk was approved.
    r_multiple: Decimal | None
    exit_reason: str | None

    @property
    def holding_seconds(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds()


class AssumptionCheck(_Model):
    key: Key
    held: bool
    note: str | None = None


class Review(_Entry):
    """The agent's verdict on a decision, after the fact (daily review, ADR-0010)."""

    type: Literal["review"] = "review"
    decision_id: str
    # The scenario that played out; ``None`` when none of them did.
    realized_scenario: Key | None
    assumptions: tuple[AssumptionCheck, ...] = ()
    thesis_held: bool | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _unique(self) -> Self:
        keys = [a.key for a in self.assumptions]
        if len(set(keys)) != len(keys):
            raise ValueError("each assumption is checked once per review")
        return self


JournalEntry = Annotated[
    SessionStarted
    | DecisionMade
    | RiskVerdictRecorded
    | OrderLinked
    | ActionFailed
    | TradeOutcome
    | Review,
    Field(discriminator="type"),
]
