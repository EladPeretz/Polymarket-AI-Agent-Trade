"""Replay the playbooks over history and take every setup as it stands (L0).

No LLM is involved (overview §5): the backtest screens the playbooks, not the
agent. Each playbook runs on its own book, one position per symbol at a time,
so its result does not depend on what the others did. Like the trigger
detector, a setup is taken when it appears (edge-triggered): one still showing
after its trade closed is not taken again until it has gone away and come back.

Every minute of the trading window, features are computed exactly as live
(:func:`build_features` over the bars before that minute), the playbooks are
evaluated, and the minute's bar is then played against open orders and
positions. The fill model is the paper simulator's, reduced to minute bars
(ADR-0005), and conservative where the bars cannot tell:

* **Marketable entry** (momentum scalp): at the next minute's first mark, plus
  ``backtest.marketable_slippage_bp``, taker fee.
* **Limit entry**: only when a traded minute's range went **through** the
  limit (touching is not enough), at the limit, maker fee; cancelled after
  ``execution.entry_timeout_seconds``.
* **Stop and target** trigger on the mark bars and fill as market orders at the
  trigger (or the bar's open, if it gapped through) less
  ``risk.stop_slippage_bp``, taker fee. A bar that reaches both hits the stop;
  in the bar a limit entry filled only the stop is checked.
* **Time stop and window end** at the minute's first mark, same slippage and fee.
* **Funding** at each settlement while the position is open, at the mark.

Size is the setup's share (``max_size``) of the per-trade risk budget,
``risk.per_trade_risk_fraction`` × ``sim.starting_collateral``, over the loss
per unit at the stop (costs included), capped at ``risk.max_position_notional``.
Results are in R as well, which does not depend on size. The account-level
limits (daily loss, trades per day, concurrent positions) are not applied:
Gate 1 judges each playbook version alone.
"""

import logging
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from perps_agent.backtest.data import DayData
from perps_agent.calendar import TradingCalendar, TradingWindow
from perps_agent.config.models import AppConfig
from perps_agent.risk import Direction, FeeRates
from perps_agent.strategy.bars import MINUTE, Bar
from perps_agent.strategy.features import BP, SessionSummary, summarize_session
from perps_agent.strategy.history import prior_sessions
from perps_agent.strategy.playbooks import Playbook, PlaybookContext, Setup
from perps_agent.strategy.regime import Regime
from perps_agent.strategy.snapshot import LOOKBACK, FeatureSnapshot, build_features
from perps_agent.venue import FundingRate

log = logging.getLogger(__name__)

ZERO = Decimal(0)


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    TIME_STOP = "time_stop"
    WINDOW_END = "window_end"


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    playbook: str
    symbol: str
    day: date
    direction: Direction
    variant: str | None
    conviction: str
    reasons: tuple[str, ...]
    signal_at: datetime
    entry_at: datetime
    entry_price: Decimal
    marketable: bool
    stop_price: Decimal
    target_price: Decimal | None
    time_stop: datetime
    quantity: Decimal
    # What the trade loses at the stop, costs included: 1R.
    risk: Decimal
    exit_at: datetime
    exit_price: Decimal
    exit_reason: ExitReason
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    entry_regime: Regime
    # The session's regime at the window end, which Gate 1 judges by.
    session_regime: Regime

    @property
    def playbook_id(self) -> str:
        return self.playbook.split("@", 1)[0]

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.fees + self.funding

    @property
    def net_r(self) -> Decimal:
        return self.net_pnl / self.risk


@dataclass(slots=True)
class PlaybookTally:
    """Setups taken and how many never filled, per playbook version."""

    signals: int = 0
    unfilled: int = 0


@dataclass(frozen=True, slots=True)
class SessionInfo:
    symbol: str
    day: date
    regime: Regime


@dataclass(slots=True)
class BacktestResult:
    start: date
    end: date
    playbooks: tuple[Playbook[Any], ...]
    trades: list[BacktestTrade] = field(default_factory=list)
    tallies: dict[str, PlaybookTally] = field(default_factory=dict)
    sessions: list[SessionInfo] = field(default_factory=list)


@dataclass(slots=True)
class _Working:
    setup: Setup
    signal_at: datetime
    entry_regime: Regime


@dataclass(slots=True)
class _Open:
    setup: Setup
    signal_at: datetime
    entry_regime: Regime
    entry_at: datetime
    entry_price: Decimal
    marketable: bool
    quantity: Decimal
    risk: Decimal
    entry_fee: Decimal
    funding: Decimal = ZERO


@dataclass(slots=True)
class _Book:
    """One playbook's state on one symbol."""

    active: frozenset[Direction] = frozenset()
    working: _Working | None = None
    open: _Open | None = None


class Backtester:
    def __init__(
        self,
        config: AppConfig,
        calendar: TradingCalendar,
        playbooks: Sequence[Playbook[Any]],
    ) -> None:
        unset = [f"risk.{name}" for name in config.risk.unset_fields()]
        if config.sim.starting_collateral is None:
            unset.append("sim.starting_collateral")
        fraction, collateral = config.risk.per_trade_risk_fraction, config.sim.starting_collateral
        if unset or fraction is None or collateral is None:
            raise ValueError(f"not set: {', '.join(unset)}")
        self._config = config
        self._calendar = calendar
        self._playbooks = tuple(playbooks)
        self._fees = FeeRates(taker=config.sim.taker_fee_rate, maker=config.sim.maker_fee_rate)
        self._budget = fraction * collateral
        self._exit_slippage = config.risk.stop_slippage_bp / BP
        self._entry_slippage = config.backtest.marketable_slippage_bp / BP
        self._timeout = timedelta(seconds=config.execution.entry_timeout_seconds)

    def run(
        self,
        data: Mapping[str, Mapping[date, DayData]],
        days: Sequence[date],
    ) -> BacktestResult:
        """Replay ``days`` for every symbol in ``data`` (which also holds the history days)."""
        days = sorted(days)
        result = BacktestResult(start=days[0], end=days[-1], playbooks=self._playbooks)
        for playbook in self._playbooks:
            result.tallies[playbook.tag] = PlaybookTally()
        for symbol, by_day in data.items():
            for day in days:
                day_data = by_day.get(day)
                window = self._calendar.trading_window(day)
                if day_data is None or window is None or not day_data.marks:
                    continue
                history = self._history(by_day, day)
                trades, regime = self._run_day(day_data, window, history, result.tallies)
                result.trades += trades
                result.sessions.append(SessionInfo(symbol, day, regime))
            log.info("backtested symbol", extra={"symbol": symbol})
        return result

    def _history(self, by_day: Mapping[date, DayData], day: date) -> tuple[SessionSummary, ...]:
        """Prior-session summaries from trade candles only, as ``load_history`` builds them live."""
        exchange = self._calendar.exchange
        summaries = []
        for prior in prior_sessions(exchange, day, self._config.features.history_sessions):
            market = exchange.trading_day(prior)
            data = by_day.get(prior)
            if market is None or data is None:
                continue
            summary = summarize_session(data.candles, market, self._config.features)
            if summary is not None:
                summaries.append(summary)
        return tuple(summaries)

    # --- One symbol, one day ---

    def _run_day(
        self,
        data: DayData,
        window: TradingWindow,
        history: tuple[SessionSummary, ...],
        tallies: dict[str, PlaybookTally],
    ) -> tuple[list[BacktestTrade], Regime]:
        bars = data.feature_bars()
        starts = [b.start for b in bars]
        marks = {b.start: b for b in data.marks}
        traded = {b.start: b for b in data.candles}
        context = PlaybookContext(window=window, risk=self._config.risk, fees=self._fees)
        books = {p.tag: _Book() for p in self._playbooks}
        trades: list[BacktestTrade] = []
        funding = sorted(data.funding, key=lambda f: f.timestamp)
        last_mark: Bar | None = None

        now = window.start
        while now <= window.end:
            if now < window.end:
                features = self._features(data.symbol, bars, starts, marks, now, window, history)
                if features is not None:
                    self._signals(features, context, books, tallies)
            bar = marks.get(now)
            if bar is not None:
                last_mark = bar
            for tag, book in books.items():
                if book.working is not None:
                    self._try_fill(book, bar, traded.get(now), now, tallies[tag])
                if book.open is None:
                    continue
                self._settle_funding(book.open, funding, marks, now)
                closed = (
                    self._close_out(book.open, bar or last_mark, now)
                    if now >= window.end
                    else self._exits(book.open, bar, now)
                )
                if closed is not None:
                    trades.append(closed)
                    book.open = None
            now += MINUTE

        final = self._features(data.symbol, bars, starts, marks, window.end, window, history)
        regime = final.regime if final is not None else Regime(None, None, None)
        return [replace(t, session_regime=regime) for t in trades], regime

    def _features(
        self,
        symbol: str,
        bars: list[Bar],
        starts: list[datetime],
        marks: Mapping[datetime, Bar],
        now: datetime,
        window: TradingWindow,
        history: tuple[SessionSummary, ...],
    ) -> FeatureSnapshot | None:
        first = min(now - LOOKBACK - timedelta(minutes=5), window.market.open - LOOKBACK)
        visible = bars[bisect_left(starts, first) : bisect_left(starts, now)]
        if not visible:
            return None
        # Live features price at the mark; the last minute's closing mark here.
        previous = marks.get(now - MINUTE)
        price = previous.close if previous is not None else visible[-1].close
        return build_features(
            symbol,
            bars=visible,
            price=price,
            now=now,
            window=window,
            history=history,
            config=self._config.features,
        )

    def _signals(
        self,
        features: FeatureSnapshot,
        context: PlaybookContext,
        books: dict[str, _Book],
        tallies: dict[str, PlaybookTally],
    ) -> None:
        for playbook in self._playbooks:
            book = books[playbook.tag]
            setup = playbook.evaluate(features, context)
            active = frozenset({setup.direction}) if setup is not None else frozenset()
            fresh = setup is not None and setup.direction not in book.active
            book.active = active
            if not fresh or book.working is not None or book.open is not None:
                continue
            assert setup is not None
            book.working = _Working(setup, features.at, features.regime)
            tallies[playbook.tag].signals += 1

    # --- Fills ---

    def _try_fill(
        self,
        book: _Book,
        mark: Bar | None,
        trade: Bar | None,
        now: datetime,
        tally: PlaybookTally,
    ) -> None:
        working = book.working
        assert working is not None
        setup = working.setup
        sign = setup.direction.sign
        if setup.entry_price is None:
            book.working = None
            if mark is None:
                # No price this minute: the marketable order is not sent.
                tally.unfilled += 1
                return
            price = mark.open * (1 + sign * self._entry_slippage)
            book.open = self._opened(working, price, now, marketable=True)
            return
        limit = setup.entry_price
        through = trade is not None and (
            trade.low < limit if setup.direction is Direction.LONG else trade.high > limit
        )
        if through:
            book.working = None
            book.open = self._opened(working, limit, now, marketable=False)
            return
        if now + MINUTE >= working.signal_at + self._timeout:
            book.working = None
            tally.unfilled += 1

    def _opened(
        self, working: _Working, price: Decimal, now: datetime, *, marketable: bool
    ) -> _Open:
        """The position after a fill at ``price``, sized as the risk engine sizes it.

        The size comes from the intended entry (the setup's price, plus the
        expected slippage if marketable), so a worse fill shows up as a loss
        beyond 1R rather than a smaller position.
        """
        setup = working.setup
        sign = setup.direction.sign
        entry_rate = self._fees.taker if marketable else self._fees.maker
        exit_rate = self._fees.taker + self._exit_slippage
        intended = setup.reference_price
        if marketable:
            intended *= 1 + sign * self._entry_slippage
        per_unit = (
            abs(intended - setup.stop_price) + intended * entry_rate + setup.stop_price * exit_rate
        )
        quantity = self._budget * setup.max_size / per_unit
        cap = self._config.risk.max_position_notional
        if cap is not None:
            quantity = min(quantity, cap / intended)
        return _Open(
            setup=setup,
            signal_at=working.signal_at,
            entry_regime=working.entry_regime,
            entry_at=now,
            entry_price=price,
            marketable=marketable,
            quantity=quantity,
            risk=per_unit * quantity,
            entry_fee=price * quantity * entry_rate,
        )

    # --- Exits ---

    def _exits(self, position: _Open, bar: Bar | None, now: datetime) -> BacktestTrade | None:
        if bar is None:
            return None
        setup = position.setup
        long = setup.direction is Direction.LONG
        if now >= setup.time_stop:
            return self._closed(position, bar.open, now, ExitReason.TIME_STOP)
        stop = setup.stop_price
        if (bar.low <= stop) if long else (bar.high >= stop):
            fill = min(stop, bar.open) if long else max(stop, bar.open)
            return self._closed(position, fill, now, ExitReason.STOP)
        # A limit entry filled somewhere in this bar: what came first is unknown,
        # so the target counts only from the next bar.
        if position.entry_at == now and not position.marketable:
            return None
        target = setup.target_price
        if target is not None and ((bar.high >= target) if long else (bar.low <= target)):
            fill = max(target, bar.open) if long else min(target, bar.open)
            return self._closed(position, fill, now, ExitReason.TARGET)
        return None

    def _close_out(self, position: _Open, bar: Bar | None, now: datetime) -> BacktestTrade:
        price = (
            position.entry_price if bar is None else (bar.open if bar.start == now else bar.close)
        )
        return self._closed(position, price, now, ExitReason.WINDOW_END)

    def _settle_funding(
        self,
        position: _Open,
        funding: Sequence[FundingRate],
        marks: Mapping[datetime, Bar],
        now: datetime,
    ) -> None:
        """Funding settled during the minute starting ``now`` while the position was open."""
        for rate in funding:
            if not now <= rate.timestamp < now + MINUTE or rate.timestamp <= position.entry_at:
                continue
            bar = marks.get(now)
            mark = bar.open if bar is not None else position.entry_price
            # A positive rate: longs pay shorts.
            position.funding -= position.setup.direction.sign * position.quantity * mark * rate.rate

    def _closed(
        self, position: _Open, trigger: Decimal, now: datetime, reason: ExitReason
    ) -> BacktestTrade:
        """The trade, exited with a market order at ``trigger`` less the slippage."""
        setup = position.setup
        sign = setup.direction.sign
        price = trigger * (1 - sign * self._exit_slippage)
        quantity = position.quantity
        exit_fee = price * quantity * self._fees.taker
        return BacktestTrade(
            playbook=setup.playbook,
            symbol=setup.symbol,
            day=position.signal_at.astimezone(self._config.session.zone).date(),
            direction=setup.direction,
            variant=setup.variant,
            conviction=str(setup.conviction),
            reasons=setup.reasons,
            signal_at=position.signal_at,
            entry_at=position.entry_at,
            entry_price=position.entry_price,
            marketable=position.marketable,
            stop_price=setup.stop_price,
            target_price=setup.target_price,
            time_stop=setup.time_stop,
            quantity=quantity,
            risk=position.risk,
            exit_at=now,
            exit_price=price,
            exit_reason=reason,
            gross_pnl=(price - position.entry_price) * quantity * sign,
            fees=position.entry_fee + exit_fee,
            funding=position.funding,
            entry_regime=position.entry_regime,
            session_regime=position.entry_regime,
        )
