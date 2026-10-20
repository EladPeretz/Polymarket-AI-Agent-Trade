"""Sizing and liquidation arithmetic (overview §3.7 rules 4–6). Pure functions.

Positions are isolated with ``margin = notional / leverage`` and maintenance
margin ``notional × mmr`` (venue research §9), the model ``sim.account`` uses.
Solving ``margin + uPnL = MM`` for the mark gives the liquidation price:

* long:  ``entry × (1 − 1/L) / (1 − mmr)``
* short: ``entry × (1 + 1/L) / (1 + mmr)``

so the distance to liquidation is ``entry × (1/L − mmr) / (1 ∓ mmr)``.
"""

from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal

from perps_agent.risk.intent import Direction
from perps_agent.venue import BookLevel


def liquidation_price(
    direction: Direction, entry: Decimal, leverage: Decimal, mmr: Decimal
) -> Decimal:
    inverse = 1 / leverage
    if direction is Direction.LONG:
        return entry * (1 - inverse) / (1 - mmr)
    return entry * (1 + inverse) / (1 + mmr)


def max_leverage_for_stop(
    direction: Direction, entry: Decimal, stop: Decimal, mmr: Decimal, buffer: Decimal
) -> int:
    """Highest whole leverage at which ``|entry − stop| ≤ buffer × |entry − liquidation|``.

    0 means no leverage keeps the stop inside the buffer.
    """
    distance = abs(entry - stop) / entry
    factor = 1 - mmr if direction is Direction.LONG else 1 + mmr
    # 1/L ≥ mmr + distance × factor / buffer
    floor = mmr + distance * factor / buffer
    if floor <= 0:
        raise ValueError("stop distance and maintenance margin cannot both be zero")
    return int((1 / floor).to_integral_value(rounding=ROUND_FLOOR))


def quantity_for_risk(budget: Decimal, entry: Decimal, stop: Decimal) -> Decimal:
    """Unrounded quantity that loses ``budget`` if the stop fills at its price."""
    return budget / abs(entry - stop)


def sweep_price(levels: Sequence[BookLevel], quantity: Decimal) -> tuple[Decimal, Decimal] | None:
    """The worst price reached taking ``quantity`` from ``levels`` (best first), and how
    much those levels hold up to it. ``None`` for an empty side.

    If the levels cannot fill ``quantity``, the price is the last level's and the
    quantity is the whole side.
    """
    if not levels:
        return None
    total = Decimal(0)
    for level in levels:
        total += level.quantity
        if total >= quantity:
            return level.price, total
    return levels[-1].price, total
