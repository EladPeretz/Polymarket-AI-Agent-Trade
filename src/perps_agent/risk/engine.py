"""Deterministic pre-trade risk engine (overview §3.7, ADR-0002).

The agent proposes a :class:`TradeIntent`; the engine approves it, resizes it
down (never up), or rejects it, and returns the exact venue order it approved.
Execution sends that order and nothing else. The agent can read the verdict and
the remaining budget but cannot change any parameter.

Every check runs and every failure is reported, so the agent sees all reasons
at once. Rules, by the numbers of §3.7:

1. Entry window open and before the cutoff; symbol and autonomy level enabled;
   instrument not close-only or settled.
2. Snapshot fresh and no older than ``max_snapshot_age_ms``; spread within
   ``max_spread_bp``; top-of-book notional on both sides ≥ ``min_top_of_book_notional``.
3. Stop on the correct side and at least the volatility stop floor from
   entry: ``stop_floor_range_multiple`` × the symbol's recent 15-minute range,
   and never closer than ``min_stop_distance_bp``. Without a 15-minute range
   the floor is unknown and the intent is rejected. Target, if any, on the
   correct side and worth at least ``min_reward_risk`` times the risk (both
   after costs, at the final entry price); time stop inside the window.
4. ``qty = budget × size / loss per unit``, rounded down, where the loss per
   unit is ``|entry − stop|`` plus the entry fee and the stop's taker fee and
   ``stop_slippage_bp``; notional ≥ the venue minimum and ≤
   ``max_position_notional``. A marketable entry is priced at the worst level
   its size reaches in the book, and that price is its limit.
5. Leverage ≤ ``max_leverage`` and the venue tier cap; isolated margin; the
   initial margin must fit in available margin (else resized down).
6. Liquidation buffer: ``|entry − stop| ≤ liquidation_buffer_fraction ×
   |entry − liquidation|``. Leverage is lowered until it holds; if no leverage
   ≥ 1 does, the intent is rejected.
7. One position per symbol, ``max_concurrent_positions`` (working entries
   count), ``max_trades_per_day``, ``max_l2_trades_per_day``.
8. Daily loss limit, a cap on the day's worst case: once the day's PnL reaches
   ``−daily_loss_limit_fraction × day_start_equity``, no new entries for the
   day. Before that, a new budget is cut to the room left under the limit after
   what open positions and working entries would lose at their stops, so the
   day cannot lose more than the limit if every stop fills at its price. Exits
   are never blocked here.
9. Venue order rules (``venue.rules.check_order``).
10. Adding (:meth:`RiskEngine.evaluate_add`): the position must be in profit,
    and the whole position's risk to the stop must stay within its approved
    budget.
11. Stops only tighten (:meth:`RiskEngine.check_stop_change`).

L2 (discretionary) intents use ``l2_risk_fraction`` of the per-trade budget.
The budget is a fraction of the lower of current and start-of-day equity.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from perps_agent.calendar import TradingCalendar
from perps_agent.clock import to_utc
from perps_agent.config.models import RiskConfig, SymbolConfig
from perps_agent.market import MarketSnapshot
from perps_agent.risk.intent import AutonomyLevel, TradeIntent
from perps_agent.risk.sizing import (
    loss_per_unit,
    max_leverage_for_stop,
    quantity_for_risk,
    stop_floor,
    sweep_price,
)
from perps_agent.risk.state import PositionRisk, RiskState
from perps_agent.venue import Instrument, OrderRequest, Side, TimeInForce
from perps_agent.venue.models import UtcDatetime
from perps_agent.venue.rules import check_order, round_price, round_quantity


class RiskCheck(StrEnum):
    SYMBOL_DISABLED = "symbol_disabled"
    AUTONOMY_LEVEL = "autonomy_level"
    INSTRUMENT_CLOSED = "instrument_closed"
    ENTRY_WINDOW = "entry_window"
    STALE_MARKET = "stale_market"
    NO_MARKET = "no_market"
    SPREAD = "spread"
    DEPTH = "depth"
    POSITION_EXISTS = "position_exists"
    NO_POSITION = "no_position"
    NOT_IN_PROFIT = "not_in_profit"
    MAX_POSITIONS = "max_positions"
    MAX_TRADES = "max_trades"
    MAX_L2_TRADES = "max_l2_trades"
    DAILY_LOSS = "daily_loss"
    STOP_SIDE = "stop_side"
    STOP_DISTANCE = "stop_distance"
    STOP_VOLATILITY = "stop_volatility"
    STOP_WIDENED = "stop_widened"
    TARGET_SIDE = "target_side"
    REWARD_RISK = "reward_risk"
    TIME_STOP = "time_stop"
    ADD_RISK = "add_risk"
    BOOK_DEPTH = "book_depth"
    NOTIONAL_CAP = "notional_cap"
    MARGIN = "margin"
    LIQUIDATION_BUFFER = "liquidation_buffer"
    SIZE_TOO_SMALL = "size_too_small"
    VENUE_RULE = "venue_rule"


class Decision(StrEnum):
    APPROVED = "approved"
    RESIZED = "resized"
    REJECTED = "rejected"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskReason(_Model):
    check: RiskCheck
    detail: str


class RiskVerdict(_Model):
    """The engine's answer to one intent.

    ``reasons`` lists every failed check for a rejection, or what reduced the
    size for a resize. ``order`` is the only order execution may send for it.
    """

    intent: TradeIntent
    adding: bool
    decision: Decision
    reasons: tuple[RiskReason, ...]
    evaluated_at: UtcDatetime
    order: OrderRequest | None = None
    leverage: int | None = None
    # Prices on the venue's grid: the entry the size was computed at (the worst
    # level for a marketable entry) and the stop and target to protect it with.
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    # pUSD: the budget the intent asked for, and what the order loses at the stop.
    risk_budget: Decimal | None = None
    risk_amount: Decimal | None = None

    @property
    def approved(self) -> bool:
        return self.decision is not Decision.REJECTED


@dataclass(frozen=True, slots=True)
class FeeRates:
    """The account's fee rates (venue research §8), from its tier."""

    taker: Decimal
    maker: Decimal


@dataclass(frozen=True, slots=True)
class _Prices:
    entry: Decimal
    stop: Decimal
    target: Decimal | None


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        symbols: Sequence[SymbolConfig],
        calendar: TradingCalendar,
        fees: FeeRates,
        *,
        new_order_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        unset = config.unset_fields()
        if unset:
            raise ValueError(f"risk config values are not set: {', '.join(unset)}")
        self._config = config
        self._symbols = {s.symbol: s for s in symbols}
        self._calendar = calendar
        self._fees = fees
        # A stop fires a market order: taker fee plus slippage past the stop price.
        self._exit_cost_rate = fees.taker + config.stop_slippage_bp / 10_000
        self._new_order_id = new_order_id

    # --- Entries ---

    def evaluate(
        self,
        intent: TradeIntent,
        *,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        state: RiskState,
        range_15m: Decimal | None,
        now: datetime,
    ) -> RiskVerdict:
        """Check a new position's entry.

        ``range_15m`` is the symbol's recent 15-minute high-low range from the
        engine's features (never from the agent), for the volatility stop floor.
        """
        return self._evaluate(
            intent, instrument, snapshot, state, range_15m, to_utc(now), adding=False
        )

    def evaluate_add(
        self,
        intent: TradeIntent,
        *,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        state: RiskState,
        range_15m: Decimal | None,
        now: datetime,
    ) -> RiskVerdict:
        """Check an add to an open, profitable position (rule 10).

        ``intent.stop_price`` becomes the whole position's stop, so it may only
        be at or tighter than the current one.
        """
        return self._evaluate(
            intent, instrument, snapshot, state, range_15m, to_utc(now), adding=True
        )

    def entries_halted(self, state: RiskState) -> bool:
        """True once the daily loss limit is reached (rule 8)."""
        return state.day_pnl <= -self._daily_loss_limit(state)

    def risk_budget(self, state: RiskState, level: AutonomyLevel) -> Decimal:
        """The full per-trade budget (pUSD) an intent at ``level`` may use with size 1."""
        assert self._config.per_trade_risk_fraction is not None
        base = min(state.equity, state.day_start_equity) * self._config.per_trade_risk_fraction
        if level is AutonomyLevel.L2:
            base *= self._config.l2_risk_fraction
        return max(Decimal(0), base)

    def daily_loss_room(self, state: RiskState) -> Decimal:
        """How much more the day may lose (pUSD) under the daily loss limit (rule 8).

        The limit, less the day's loss so far, less what open positions (from
        their marks) and working entries would lose if their stops filled.
        """
        open_risk = sum(
            (
                p.loss_from_mark + p.quantity * p.stop_price * self._exit_cost_rate
                for p in state.positions.values()
            ),
            Decimal(0),
        )
        return self._daily_loss_limit(state) + state.day_pnl - open_risk - state.pending_risk

    def min_stop_distance(self, entry: Decimal, range_15m: Decimal) -> Decimal:
        """The volatility stop floor at ``entry`` (rule 3), in price units."""
        assert self._config.min_stop_distance_bp is not None
        return stop_floor(
            entry,
            range_15m,
            min_distance_bp=self._config.min_stop_distance_bp,
            range_multiple=self._config.stop_floor_range_multiple,
        )

    def order_risk(
        self, quantity: Decimal, price: Decimal, stop: Decimal, *, post_only: bool
    ) -> Decimal:
        """What ``quantity`` bought or sold at ``price`` loses if ``stop`` fires, costs included."""
        return quantity * self._loss_per_unit(price, stop, post_only=post_only)

    @property
    def verdict_ttl(self) -> timedelta:
        """How long an approved verdict stays valid; execution refuses older ones."""
        return timedelta(milliseconds=self._config.max_snapshot_age_ms)

    # --- Protection ---

    def check_stop_change(
        self, position: PositionRisk, new_stop: Decimal, mark: Decimal
    ) -> RiskReason | None:
        """Why a stop move (manual or trailing) is not allowed, or ``None`` (rule 11)."""
        sign = position.direction.sign
        if (new_stop - position.stop_price) * sign < 0:
            return RiskReason(
                check=RiskCheck.STOP_WIDENED,
                detail=f"stop {new_stop} is looser than the current {position.stop_price}",
            )
        if (mark - new_stop) * sign <= 0:
            return RiskReason(
                check=RiskCheck.STOP_SIDE,
                detail=f"stop {new_stop} is at or through mark {mark}; close the position instead",
            )
        return None

    # --- Internals ---

    def _evaluate(
        self,
        intent: TradeIntent,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        state: RiskState,
        range_15m: Decimal | None,
        now: datetime,
        *,
        adding: bool,
    ) -> RiskVerdict:
        if instrument.symbol != intent.symbol or snapshot.symbol != intent.symbol:
            raise ValueError(
                f"intent for {intent.symbol} checked against {instrument.symbol}/{snapshot.symbol}"
            )
        problems: list[RiskReason] = []

        def reject(check: RiskCheck, detail: str) -> None:
            problems.append(RiskReason(check=check, detail=detail))

        def rejected() -> RiskVerdict:
            return RiskVerdict(
                intent=intent,
                adding=adding,
                decision=Decision.REJECTED,
                reasons=tuple(problems),
                evaluated_at=now,
            )

        self._check_permissions(intent, instrument, now, reject)
        self._check_market(snapshot, now, reject)
        position = state.positions.get(intent.symbol)
        self._check_portfolio(intent, state, position, adding, reject)
        mark = snapshot.mark_price
        if snapshot.best_bid is None or snapshot.best_ask is None or mark is None:
            return rejected()
        prices = self._check_prices(
            intent, instrument, snapshot, position, range_15m, now, adding, reject
        )
        if problems or prices is None:
            return rejected()

        resized: list[RiskReason] = []

        def resize(check: RiskCheck, detail: str) -> None:
            resized.append(RiskReason(check=check, detail=detail))

        config = self._config
        sign = intent.direction.sign
        side = intent.direction.side
        entry, stop = prices.entry, prices.stop
        requested = self.risk_budget(state, intent.level) * intent.size
        budget = requested
        if adding:
            assert position is not None
            held = max(
                Decimal(0),
                position.quantity
                * ((position.average_entry - stop) * sign + stop * self._exit_cost_rate),
            )
            room = position.approved_risk - held
            if room <= 0:
                reject(
                    RiskCheck.ADD_RISK,
                    f"position already risks {held} of its {position.approved_risk} budget",
                )
                return rejected()
            if room < budget:
                budget = room
                resize(RiskCheck.ADD_RISK, f"budget cut to the position's remaining {room}")
        day_room = self.daily_loss_room(state)
        if day_room <= 0:
            reject(
                RiskCheck.DAILY_LOSS,
                f"open positions and entries already use the day's loss room ({day_room:.2f})",
            )
            return rejected()
        if day_room < budget:
            budget = day_room
            resize(RiskCheck.DAILY_LOSS, f"budget cut to the {day_room:.2f} left for the day")

        costs = {
            "entry_fee_rate": self._entry_fee_rate(post_only=intent.post_only),
            "exit_cost_rate": self._exit_cost_rate,
        }
        quantity = quantity_for_risk(budget, entry, stop, **costs)
        if intent.is_marketable:
            levels = snapshot.asks if side is Side.BUY else snapshot.bids
            swept = sweep_price(levels, quantity)
            assert swept is not None  # both sides were checked above
            entry, depth = swept
            quantity = quantity_for_risk(budget, entry, stop, **costs)
            if quantity > depth:
                quantity = depth
                resize(RiskCheck.BOOK_DEPTH, f"cut to the {depth} shown up to {entry}")
        if prices.target is not None:
            # The target fires a market order too: the same exit costs as the stop.
            risk = loss_per_unit(entry, stop, **costs)
            reward = abs(prices.target - entry) - entry * costs["entry_fee_rate"]
            reward -= prices.target * self._exit_cost_rate
            if reward < config.min_reward_risk * risk:
                reject(
                    RiskCheck.REWARD_RISK,
                    f"target {prices.target} is worth {reward / risk:.2f}R after costs, "
                    f"minimum {config.min_reward_risk}R",
                )
                return rejected()

        assert config.max_position_notional is not None
        held_quantity = position.quantity if adding and position is not None else Decimal(0)
        notional_cap = min(
            config.max_position_notional - held_quantity * mark,
            instrument.max_limit_notional,
        )
        if quantity * entry > notional_cap:
            quantity = max(Decimal(0), notional_cap / entry)
            resize(RiskCheck.NOTIONAL_CAP, f"notional capped at {notional_cap}")

        total = held_quantity + quantity
        average = (
            (held_quantity * position.average_entry + quantity * entry) / total
            if adding and position is not None and total > 0
            else entry
        )
        assert config.max_leverage is not None and config.liquidation_buffer_fraction is not None
        leverage = min(
            int(config.max_leverage.to_integral_value(rounding=ROUND_FLOOR)),
            instrument.max_leverage_for(total * mark),
        )
        if (average - stop) * sign > 0:
            buffered = max_leverage_for_stop(
                intent.direction,
                average,
                stop,
                instrument.maintenance_margin_rate,
                config.liquidation_buffer_fraction,
            )
            if buffered < 1:
                reject(
                    RiskCheck.LIQUIDATION_BUFFER,
                    f"stop {stop} is too far from {average}: it would not trigger well before "
                    "liquidation at any leverage",
                )
                return rejected()
            leverage = min(leverage, buffered)

        margin = quantity * entry / leverage
        if margin > state.available_margin:
            quantity = max(Decimal(0), state.available_margin * leverage / entry)
            resize(
                RiskCheck.MARGIN,
                f"initial margin {margin:.2f} > available {state.available_margin:.2f}",
            )

        quantity = round_quantity(instrument, quantity)
        if quantity <= 0 or quantity * entry < instrument.min_notional:
            reject(
                RiskCheck.SIZE_TOO_SMALL,
                f"{quantity} × {entry} is below the venue minimum {instrument.min_notional}",
            )
            return rejected()

        order = OrderRequest(
            client_order_id=self._new_order_id(),
            instrument_id=instrument.instrument_id,
            side=side,
            quantity=quantity,
            price=entry,
            time_in_force=TimeInForce.IOC if intent.is_marketable else TimeInForce.GTC,
            post_only=intent.post_only,
        )
        for violation in check_order(instrument, order, mark):
            reject(RiskCheck.VENUE_RULE, f"{violation.rule}: {violation.detail}")
        if problems:
            return rejected()

        return RiskVerdict(
            intent=intent,
            adding=adding,
            decision=Decision.RESIZED if resized else Decision.APPROVED,
            reasons=tuple(resized),
            evaluated_at=now,
            order=order,
            leverage=leverage,
            entry_price=entry,
            stop_price=stop,
            target_price=prices.target,
            risk_budget=requested,
            risk_amount=self.order_risk(quantity, entry, stop, post_only=intent.post_only),
        )

    def _check_permissions(
        self,
        intent: TradeIntent,
        instrument: Instrument,
        now: datetime,
        reject: Callable[[RiskCheck, str], None],
    ) -> None:
        symbol = self._symbols.get(intent.symbol)
        if symbol is None or not symbol.enabled:
            reject(RiskCheck.SYMBOL_DISABLED, f"{intent.symbol} is not enabled")
        elif not getattr(symbol.autonomy, intent.level.value):
            reject(RiskCheck.AUTONOMY_LEVEL, f"{intent.level} is not enabled on {intent.symbol}")
        if not instrument.accepts_new_exposure:
            reject(RiskCheck.INSTRUMENT_CLOSED, f"{instrument.symbol} is close-only or settled")
        if not self._calendar.is_entry_allowed(now):
            reject(RiskCheck.ENTRY_WINDOW, "outside the entry window")

    def _check_market(
        self, snapshot: MarketSnapshot, now: datetime, reject: Callable[[RiskCheck, str], None]
    ) -> None:
        config = self._config
        age = now - snapshot.taken_at
        if snapshot.stale:
            reject(RiskCheck.STALE_MARKET, "; ".join(snapshot.stale_reasons) or "stale")
        elif age > timedelta(milliseconds=config.max_snapshot_age_ms):
            reject(RiskCheck.STALE_MARKET, f"snapshot is {age.total_seconds():.1f}s old")
        bid, ask, spread = snapshot.best_bid, snapshot.best_ask, snapshot.spread_bp
        if bid is None or ask is None or spread is None or snapshot.mark_price is None:
            reject(RiskCheck.NO_MARKET, "no two-sided book or no mark price")
            return
        assert config.max_spread_bp is not None and config.min_top_of_book_notional is not None
        if spread > config.max_spread_bp:
            reject(RiskCheck.SPREAD, f"spread {spread:.2f} bp > {config.max_spread_bp} bp")
        depth = min(bid.price * bid.quantity, ask.price * ask.quantity)
        if depth < config.min_top_of_book_notional:
            reject(
                RiskCheck.DEPTH,
                f"top of book {depth:.2f} < {config.min_top_of_book_notional} on one side",
            )

    def _check_portfolio(
        self,
        intent: TradeIntent,
        state: RiskState,
        position: PositionRisk | None,
        adding: bool,
        reject: Callable[[RiskCheck, str], None],
    ) -> None:
        config = self._config
        if adding:
            if position is None:
                reject(RiskCheck.NO_POSITION, f"no open position in {intent.symbol}")
            elif position.direction is not intent.direction:
                reject(
                    RiskCheck.NO_POSITION, f"the {intent.symbol} position is {position.direction}"
                )
            elif position.unrealized_pnl <= 0:
                reject(RiskCheck.NOT_IN_PROFIT, "only a profitable position may be added to")
            if intent.symbol in state.pending_entries:
                reject(RiskCheck.POSITION_EXISTS, f"an entry in {intent.symbol} is still working")
        elif intent.symbol in state.exposed_symbols:
            reject(RiskCheck.POSITION_EXISTS, f"{intent.symbol} already has a position or entry")
        elif len(state.exposed_symbols) >= config.max_concurrent_positions:
            reject(RiskCheck.MAX_POSITIONS, f"{config.max_concurrent_positions} positions open")
        assert config.max_trades_per_day is not None and config.max_l2_trades_per_day is not None
        if state.trades_today >= config.max_trades_per_day:
            reject(RiskCheck.MAX_TRADES, f"{state.trades_today} entries today")
        if (
            intent.level is AutonomyLevel.L2
            and state.l2_trades_today >= config.max_l2_trades_per_day
        ):
            reject(RiskCheck.MAX_L2_TRADES, f"{state.l2_trades_today} L2 entries today")
        if self.entries_halted(state):
            limit = self._daily_loss_limit(state)
            reject(RiskCheck.DAILY_LOSS, f"day PnL {state.day_pnl:.2f} reached -{limit:.2f}")

    def _check_prices(
        self,
        intent: TradeIntent,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        position: PositionRisk | None,
        range_15m: Decimal | None,
        now: datetime,
        adding: bool,
        reject: Callable[[RiskCheck, str], None],
    ) -> _Prices | None:
        side = intent.direction.side
        sign = intent.direction.sign
        if intent.entry_price is None:
            best = snapshot.best_ask if side is Side.BUY else snapshot.best_bid
            assert best is not None
            entry = best.price
        else:
            entry = round_price(instrument, intent.entry_price, side)
        # Rounded away from the entry: a little more distance, never less.
        stop = round_price(instrument, intent.stop_price, side)
        target = (
            round_price(instrument, intent.target_price)
            if intent.target_price is not None
            else None
        )
        assert self._config.min_stop_distance_bp is not None
        ok = True
        if (entry - stop) * sign <= 0:
            reject(RiskCheck.STOP_SIDE, f"stop {stop} is not on the losing side of entry {entry}")
            ok = False
        else:
            distance = abs(entry - stop)
            distance_bp = distance / entry * 10_000
            if distance_bp < self._config.min_stop_distance_bp:
                reject(
                    RiskCheck.STOP_DISTANCE,
                    f"stop is {distance_bp:.1f} bp away, minimum "
                    f"{self._config.min_stop_distance_bp} bp",
                )
                ok = False
            elif range_15m is None:
                reject(
                    RiskCheck.STOP_VOLATILITY,
                    "no recent 15-minute range, so the volatility stop floor is unknown",
                )
                ok = False
            elif distance < (floor := self.min_stop_distance(entry, range_15m)):
                reject(
                    RiskCheck.STOP_VOLATILITY,
                    f"stop is {distance} away, inside the volatility floor {floor:.4f} "
                    f"({self._config.stop_floor_range_multiple} × the 15-minute range {range_15m})",
                )
                ok = False
        if adding and position is not None and (stop - position.stop_price) * sign < 0:
            reject(
                RiskCheck.STOP_WIDENED,
                f"stop {stop} is looser than the position's {position.stop_price}",
            )
            ok = False
        if target is not None and (target - entry) * sign <= 0:
            reject(RiskCheck.TARGET_SIDE, f"target {target} is not on the winning side of {entry}")
            ok = False
        if intent.time_stop is not None:
            window = self._calendar.window_at(now)
            if window is None or not now < intent.time_stop <= window.end:
                reject(
                    RiskCheck.TIME_STOP, f"time stop {intent.time_stop} is not inside the window"
                )
                ok = False
        return _Prices(entry, stop, target) if ok else None

    def _entry_fee_rate(self, *, post_only: bool) -> Decimal:
        # Only a post-only entry is sure to pay the maker rate.
        return self._fees.maker if post_only else self._fees.taker

    def _loss_per_unit(self, entry: Decimal, stop: Decimal, *, post_only: bool) -> Decimal:
        return loss_per_unit(
            entry,
            stop,
            entry_fee_rate=self._entry_fee_rate(post_only=post_only),
            exit_cost_rate=self._exit_cost_rate,
        )

    def _daily_loss_limit(self, state: RiskState) -> Decimal:
        assert self._config.daily_loss_limit_fraction is not None
        return state.day_start_equity * self._config.daily_loss_limit_fraction
