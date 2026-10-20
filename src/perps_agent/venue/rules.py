"""Local venue order rules (venue research §5): precision, significant figures, price
band, notional limits. Pure functions, so the risk engine reuses them (WP6) and an
order is checked before anything is signed or sent.
"""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from perps_agent.venue.models import Instrument, OrderRequest, OrderStatus, Side

# The venue accepts at most 5 significant figures for price and quantity.
MAX_SIGNIFICANT_FIGURES = 5


class Violation(StrEnum):
    INSTRUMENT_MISMATCH = "instrument_mismatch"
    INSTRUMENT_CLOSE_ONLY = "instrument_close_only"
    INSTRUMENT_SETTLED = "instrument_settled"
    PRICE_PRECISION = "price_precision"
    PRICE_SIGNIFICANT_FIGURES = "price_significant_figures"
    PRICE_OUTSIDE_BAND = "price_outside_band"
    QUANTITY_PRECISION = "quantity_precision"
    QUANTITY_SIGNIFICANT_FIGURES = "quantity_significant_figures"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    ABOVE_MAX_NOTIONAL = "above_max_notional"


# Violations the venue reports as an order status; any other one is a request error.
STATUS_FOR_VIOLATION = {
    Violation.INSTRUMENT_CLOSE_ONLY: OrderStatus.INSTRUMENT_CLOSE_ONLY,
    Violation.INSTRUMENT_SETTLED: OrderStatus.INSTRUMENT_SETTLED,
    Violation.BELOW_MIN_NOTIONAL: OrderStatus.BELOW_MIN_NOTIONAL,
    Violation.PRICE_OUTSIDE_BAND: OrderStatus.PRICE_OUTSIDE_BOUNDS,
}


@dataclass(frozen=True, slots=True)
class RuleViolation:
    rule: Violation
    detail: str


def significant_figures(value: Decimal) -> int:
    """Significant digits of ``value``, ignoring trailing zeros (``21600`` → 3)."""
    if value.is_zero():
        return 0
    return len(value.normalize().as_tuple().digits)


def decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    assert isinstance(exponent, int), "finite decimals only"
    return max(0, -exponent)


def _quantize(value: Decimal, decimals: int, rounding: str) -> Decimal:
    if value.is_zero():
        return value
    exponent = max(-decimals, value.adjusted() - (MAX_SIGNIFICANT_FIGURES - 1))
    return value.quantize(Decimal(1).scaleb(exponent), rounding=rounding)


def round_price(instrument: Instrument, price: Decimal, side: Side | None = None) -> Decimal:
    """Round to a price the venue accepts.

    With a ``side`` the rounding is passive (a buy rounds down, a sell up), so
    rounding never makes an order more aggressive than asked. Without one it
    rounds to nearest.
    """
    if side is None:
        rounding = ROUND_HALF_EVEN
    else:
        rounding = "ROUND_FLOOR" if side is Side.BUY else "ROUND_CEILING"
    return _quantize(price, instrument.price_decimals, rounding)


def round_quantity(instrument: Instrument, quantity: Decimal) -> Decimal:
    """Round a quantity down to what the venue accepts (never oversizes)."""
    return _quantize(quantity, instrument.quantity_decimals, ROUND_DOWN)


def check_order(
    instrument: Instrument, request: OrderRequest, mark_price: Decimal
) -> tuple[RuleViolation, ...]:
    """Every venue rule ``request`` breaks, given the current mark. Empty means valid.

    Reduce-only orders are not held to the minimum notional here: an exit is
    never blocked locally on size, the venue has the final word.
    """
    found: list[RuleViolation] = []

    def fail(rule: Violation, detail: str) -> None:
        found.append(RuleViolation(rule, detail))

    if request.instrument_id != instrument.instrument_id:
        fail(Violation.INSTRUMENT_MISMATCH, f"order for {request.instrument_id}")
    if instrument.settled:
        fail(Violation.INSTRUMENT_SETTLED, f"{instrument.symbol} is settled")
    elif instrument.close_only and not request.reduce_only:
        fail(Violation.INSTRUMENT_CLOSE_ONLY, f"{instrument.symbol} accepts reduce-only orders")

    quantity = request.quantity
    if decimal_places(quantity) > instrument.quantity_decimals:
        fail(
            Violation.QUANTITY_PRECISION,
            f"{quantity} has more than {instrument.quantity_decimals} decimals",
        )
    if significant_figures(quantity) > MAX_SIGNIFICANT_FIGURES:
        fail(Violation.QUANTITY_SIGNIFICANT_FIGURES, f"{quantity} has more than 5 figures")

    price = request.price
    if price is not None:
        if decimal_places(price) > instrument.price_decimals:
            fail(
                Violation.PRICE_PRECISION,
                f"{price} has more than {instrument.price_decimals} decimals",
            )
        if significant_figures(price) > MAX_SIGNIFICANT_FIGURES:
            fail(Violation.PRICE_SIGNIFICANT_FIGURES, f"{price} has more than 5 figures")
        low = mark_price * (1 - instrument.price_band)
        high = mark_price * (1 + instrument.price_band)
        if not low <= price <= high:
            fail(
                Violation.PRICE_OUTSIDE_BAND,
                f"{price} outside [{low}, {high}] (mark {mark_price} ± {instrument.price_band})",
            )

    if price is None:
        # A market-style order fills somewhere inside the price band: judge the
        # minimum at mark and the maximum at the furthest price it could reach.
        notional = quantity * mark_price
        reach = 1 + instrument.price_band if request.side is Side.BUY else Decimal(1)
        worst = notional * reach
        cap = instrument.max_market_notional
    else:
        notional = worst = quantity * price
        cap = instrument.max_limit_notional
    if not request.reduce_only and notional < instrument.min_notional:
        fail(Violation.BELOW_MIN_NOTIONAL, f"notional {notional} < {instrument.min_notional}")
    if worst > cap:
        fail(Violation.ABOVE_MAX_NOTIONAL, f"notional up to {worst} > {cap}")
    return tuple(found)
