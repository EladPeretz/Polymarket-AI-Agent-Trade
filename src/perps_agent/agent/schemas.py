"""What the agent sends to its tools (overview §3.5). Validated before anything else runs.

The agent never builds a risk-engine ``TradeIntent`` itself: a
:class:`TradeProposal` names its *source* (a playbook or ``discretionary``) and
the bridge decides the autonomy level (ADR-0003), checks the Gate 1 status of
the playbook and fills in the versioned playbook tag.

Every entry and add carries the decision record the journal needs for the
learning loop (ADR-0010): the scenarios weighed, their probabilities, the
chosen scenario and the expected result in R.
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from perps_agent.execution.plans import ExitPlan, TrailingRule
from perps_agent.journal import Scenario, ToolCall
from perps_agent.journal.entries import Key, Probability, Text
from perps_agent.risk import Direction

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
Fraction = Annotated[Decimal, Field(gt=0, le=1)]
DISCRETIONARY = "discretionary"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Decision(_Model):
    """The reasoning behind an entry or add, for the journal (ADR-0010)."""

    thesis: Text
    confidence: Probability
    scenarios: Annotated[tuple[Scenario, ...], Field(min_length=1)]
    chosen_scenario: Key
    # Probability-weighted result of the plan, in R.
    expected_r: Decimal | None = None
    regime: Key | None = None
    setup: Key | None = None
    # The event this answers (from next_event); omitted when acting on initiative.
    event_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class _Order(_Model):
    """Entry order: ``limit`` (the default) at ``limit_price``, or ``market`` (marketable)."""

    order_type: Literal["limit", "market"] = "limit"
    limit_price: PositiveDecimal | None = None
    post_only: bool = False
    # Fraction (0–1] of the per-trade risk budget.
    size: Fraction
    stop_price: PositiveDecimal
    # How long a limit entry may work before it is cancelled; omitted: the
    # configured default. The engine caps it at ``max_entry_timeout_seconds``.
    entry_timeout_seconds: Annotated[int, Field(ge=10, le=3_600)] | None = None

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("a limit order needs limit_price")
        if self.order_type == "market" and (self.limit_price is not None or self.post_only):
            raise ValueError("a market order takes no limit_price and cannot be post_only")
        if self.order_type == "market" and self.entry_timeout_seconds is not None:
            raise ValueError("entry_timeout_seconds applies to limit orders only")
        return self

    @property
    def entry_price(self) -> Decimal | None:
        return self.limit_price if self.order_type == "limit" else None


class TradeProposal(_Order, _Decision):
    symbol: str
    side: Direction
    # "discretionary", or "playbook:<id>" for a playbook's setup.
    source: Annotated[str, Field(pattern=r"^(discretionary|playbook:[a-z0-9_]+)$")]
    target_price: PositiveDecimal | None = None
    # The position is closed this many minutes after entry (and always by the window end).
    hold_minutes: Annotated[int, Field(ge=1, le=180)] | None = None
    exit_plan: ExitPlan | None = None

    @property
    def playbook_id(self) -> str | None:
        return None if self.source == DISCRETIONARY else self.source.split(":", 1)[1]


class AddToPosition(_Order, _Decision):
    """Add to a profitable position. ``stop_price`` becomes the whole position's stop."""

    symbol: str
    target_price: PositiveDecimal | None = None


class AdjustProtection(_Model):
    symbol: str
    stop_price: PositiveDecimal | None = None
    target_price: PositiveDecimal | None = None
    remove_target: bool = False
    reason: Text
    event_id: str | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.stop_price is None and self.target_price is None and not self.remove_target:
            raise ValueError("give stop_price, target_price or remove_target")
        if self.target_price is not None and self.remove_target:
            raise ValueError("either move the target or remove it")
        return self


class UpdateExitPlan(_Model):
    symbol: str
    # ``None`` removes the plan.
    plan: ExitPlan | None
    reason: Text
    event_id: str | None = None


class SetTrailingStop(_Model):
    symbol: str
    # ``None`` stops trailing; the rest of the exit plan stays.
    rule: TrailingRule | None
    reason: Text
    event_id: str | None = None


class ReducePosition(_Model):
    symbol: str
    fraction: Fraction
    reason: Text
    event_id: str | None = None


class ClosePosition(_Model):
    symbol: str
    reason: Text
    event_id: str | None = None


class JournalNote(_Model):
    """Reasoning not tied to an order: a note, or a decision to stand aside."""

    note: Text
    stand_aside: bool = False
    symbol: str | None = None
    event_id: str | None = None
    scenarios: tuple[Scenario, ...] = ()
    regime: Key | None = None
    setup: Key | None = None


class NextEvent(_Model):
    timeout_seconds: Annotated[int, Field(ge=0, le=540)] | None = None


class SymbolArg(_Model):
    symbol: str


class NoArgs(_Model):
    pass
