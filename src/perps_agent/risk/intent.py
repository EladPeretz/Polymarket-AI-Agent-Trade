"""The ``TradeIntent`` the agent proposes (overview §3.5), validated before risk.

The agent sizes in **risk units**: ``size`` is the fraction (0–1) of the
per-trade risk budget it wants to use. The risk engine turns that budget and
the stop distance into a quantity, so the agent cannot oversize by mistake.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from perps_agent.venue import Side
from perps_agent.venue.models import UtcDatetime

PositiveDecimal = Annotated[Decimal, Field(gt=0)]


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def side(self) -> Side:
        """The order side that opens (or adds to) a position in this direction."""
        return Side.BUY if self is Direction.LONG else Side.SELL

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class AutonomyLevel(StrEnum):
    """ADR-0003: L0 filters playbook signals, L1 picks a playbook, L2 is discretionary."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class TradeIntent(BaseModel):
    """A proposed entry (or add). ``entry_price`` ``None`` means marketable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    direction: Direction
    level: AutonomyLevel
    # Playbook id (and version) for L0/L1; L2 trades are not tied to a playbook.
    playbook: str | None = None
    entry_price: PositiveDecimal | None = None
    post_only: bool = False
    size: Annotated[Decimal, Field(gt=0, le=1)]
    stop_price: PositiveDecimal
    target_price: PositiveDecimal | None = None
    time_stop: UtcDatetime | None = None
    thesis: Annotated[str, Field(min_length=1)]
    confidence: Annotated[Decimal, Field(ge=0, le=1)]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.level is AutonomyLevel.L2 and self.playbook is not None:
            raise ValueError("an L2 (discretionary) intent is not tied to a playbook")
        if self.level is not AutonomyLevel.L2 and not self.playbook:
            raise ValueError(f"an {self.level} intent needs a playbook id")
        if self.post_only and self.entry_price is None:
            raise ValueError("post_only needs a limit entry price")
        return self

    @property
    def is_marketable(self) -> bool:
        return self.entry_price is None
