"""Simulated account: collateral, isolated positions, margin and funding (venue research §7, §9).

Every position is isolated (overview §3.7): it holds its own margin, set at the
leverage in force when it was opened or added to, and is liquidated on its own
when ``margin + unrealized PnL`` falls below maintenance margin.

* ``Equity = collateral + Σ unrealized PnL(mark)``. Fees, realized PnL and
  funding are booked to collateral as they happen.
* ``IM = notional / leverage``; ``MM = notional × maintenance_margin_rate``
  (``0.5 / max_leverage``, flat 5% on stock perps).
* Funding: at each settlement a position pays ``quantity × mark × rate``
  (signed: longs pay when the rate is positive). It also moves the position's
  isolated margin.
"""

from dataclasses import dataclass
from decimal import Decimal

from perps_agent.venue import Instrument, Side
from perps_agent.venue.models import Position


@dataclass(slots=True)
class SimPosition:
    instrument: Instrument
    # Signed: > 0 long, < 0 short.
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal
    margin: Decimal

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        return self.quantity * (mark - self.entry_price)

    def notional(self, mark: Decimal) -> Decimal:
        return abs(self.quantity) * mark

    def maintenance_margin(self, mark: Decimal) -> Decimal:
        return self.notional(mark) * self.instrument.maintenance_margin_rate

    def margin_equity(self, mark: Decimal) -> Decimal:
        """Isolated margin plus unrealized PnL: what stands between it and liquidation."""
        return self.margin + self.unrealized_pnl(mark)

    def is_liquidatable(self, mark: Decimal) -> bool:
        return self.margin_equity(mark) < self.maintenance_margin(mark)

    def liquidation_price(self) -> Decimal:
        """Mark at which ``margin + uPnL = MM`` (before funding moves the margin)."""
        q, mmr = self.quantity, self.instrument.maintenance_margin_rate
        # margin + q (p − e) = |q| p mmr  →  p = (q e − margin) / (q − |q| mmr)
        return (q * self.entry_price - self.margin) / (q - abs(q) * mmr)

    def to_domain(self) -> Position:
        return Position(
            instrument_id=self.instrument.instrument_id,
            quantity=self.quantity,
            entry_price=self.entry_price,
            leverage=self.leverage,
            isolated=True,
        )


@dataclass(frozen=True, slots=True)
class FillEffect:
    realized_pnl: Decimal
    opened: Decimal
    closed: Decimal


class SimAccount:
    def __init__(self, collateral: Decimal) -> None:
        self.collateral = collateral
        self.positions: dict[int, SimPosition] = {}
        self.realized_pnl = Decimal(0)
        self.fees_paid = Decimal(0)
        self.funding_paid = Decimal(0)

    def equity(self, marks: dict[int, Decimal]) -> Decimal:
        return self.collateral + sum(
            (p.unrealized_pnl(marks.get(iid, p.entry_price)) for iid, p in self.positions.items()),
            Decimal(0),
        )

    def position_quantity(self, instrument_id: int) -> Decimal:
        position = self.positions.get(instrument_id)
        return position.quantity if position else Decimal(0)

    def apply_fill(
        self,
        instrument: Instrument,
        side: Side,
        price: Decimal,
        quantity: Decimal,
        fee: Decimal,
        leverage: Decimal,
    ) -> FillEffect:
        """Book a fill: update the position, realize PnL on the reduced part, pay the fee."""
        iid = instrument.instrument_id
        signed = quantity * side.sign
        position = self.positions.get(iid)
        realized = Decimal(0)
        closed = Decimal(0)
        opening = quantity
        if position is not None and position.quantity * signed < 0:
            closed = min(quantity, abs(position.quantity))
            sign = 1 if position.quantity > 0 else -1
            realized = closed * (price - position.entry_price) * sign
            position.margin -= position.margin * closed / abs(position.quantity)
            position.quantity += closed * side.sign
            opening = quantity - closed
            if position.quantity == 0:
                del self.positions[iid]
                position = None
        if opening > 0:
            added_margin = opening * price / leverage
            if position is None:
                self.positions[iid] = SimPosition(
                    instrument=instrument,
                    quantity=opening * side.sign,
                    entry_price=price,
                    leverage=leverage,
                    margin=added_margin,
                )
            else:
                size = abs(position.quantity)
                position.entry_price = (size * position.entry_price + opening * price) / (
                    size + opening
                )
                position.quantity += opening * side.sign
                position.margin += added_margin
                position.leverage = leverage
        self.collateral += realized - fee
        self.realized_pnl += realized
        self.fees_paid += fee
        return FillEffect(realized_pnl=realized, opened=opening, closed=closed)

    def apply_funding(self, instrument_id: int, mark: Decimal, rate: Decimal) -> Decimal | None:
        """Settle one funding interval. Returns the amount received (negative: paid)."""
        position = self.positions.get(instrument_id)
        if position is None:
            return None
        amount = -position.quantity * mark * rate
        self.collateral += amount
        self.funding_paid -= amount
        position.margin += amount
        return amount
