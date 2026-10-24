"""Pure fill-model functions (overview §3.9, ADR-0005).

* A marketable order walks the opposite side of the book, level by level, up to
  its limit; what the book cannot fill stays unfilled.
* A resting limit fills only when a public trade prints **through** its price
  (strictly better than the limit for the trade's counterparty). Touching the
  price is not enough and no queue position is assumed.
* Fees are ``|price × quantity| × rate`` per fill (venue research §8).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from perps_agent.venue import Side


@dataclass(slots=True)
class Level:
    """A mutable book level, so the simulator can consume liquidity it has taken."""

    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class Execution:
    price: Decimal
    quantity: Decimal


def within_limit(side: Side, price: Decimal, limit: Decimal | None) -> bool:
    """Whether a buy (sell) may trade at ``price`` given its limit. ``None``: no limit."""
    if limit is None:
        return True
    return price <= limit if side is Side.BUY else price >= limit


def available(levels: Sequence[Level], side: Side, limit: Decimal | None) -> Decimal:
    """Quantity a ``side`` order can take from ``levels`` (the opposite side) within ``limit``."""
    return sum(
        (level.quantity for level in levels if within_limit(side, level.price, limit)),
        Decimal(0),
    )


def walk_book(
    levels: list[Level], side: Side, quantity: Decimal, limit: Decimal | None
) -> list[Execution]:
    """Take up to ``quantity`` from ``levels`` (best first), consuming what is taken.

    ``levels`` is the side the order trades against: asks for a buy, bids for a
    sell. Emptied levels are removed.
    """
    executions: list[Execution] = []
    remaining = quantity
    while remaining > 0 and levels and within_limit(side, levels[0].price, limit):
        level = levels[0]
        take = min(remaining, level.quantity)
        if take > 0:
            executions.append(Execution(level.price, take))
            level.quantity -= take
            remaining -= take
        if level.quantity <= 0:
            levels.pop(0)
    return executions


def trades_through(side: Side, limit: Decimal, trade_price: Decimal) -> bool:
    """Whether a public trade at ``trade_price`` prints through a resting ``side`` limit."""
    return trade_price < limit if side is Side.BUY else trade_price > limit


def fee(price: Decimal, quantity: Decimal, rate: Decimal) -> Decimal:
    return abs(price * quantity) * rate
