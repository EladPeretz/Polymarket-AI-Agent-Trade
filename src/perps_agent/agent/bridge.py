"""The agent bridge (overview §3.5): the engine-side tool surface the agent uses.

The agent never talks to the venue. Each tool call is validated against its
schema (:mod:`perps_agent.agent.schemas`), journaled (ADR-0010) and carried
out through the same risk engine and execution paths every order uses
(ADR-0002). The bridge only proposes; the risk engine disposes.

* **Events.** The engine pushes triggers (playbook setups, large moves,
  position events, check-ins, session milestones) and the bridge adds what
  happens to orders and positions, read from the ledger. ``next_event`` blocks
  until something is queued (or the timeout) and returns the events with a
  compact snapshot of each symbol involved and the session state.
* **Autonomy (ADR-0003).** A ``discretionary`` proposal is L2. A playbook
  proposal must match a live setup of that playbook, and the playbook version
  must have passed the Gate 1 backtest screen; taking the setup as it stands
  is L0, changing its prices is L1, and the size may not exceed the setup's cap.
  Until Gate 1 (WP13) no playbook is screened, so playbook signals are context
  only and the agent trades them, if at all, as L2 (owner decision 2026-10-21).
* **Liveness.** ``last_call_at`` is when the agent last called a tool, for the
  guardian's agent-liveness check (WP11).
"""

import asyncio
import itertools
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from perps_agent.agent import context
from perps_agent.agent.context import Json
from perps_agent.agent.schemas import (
    AddToPosition,
    AdjustProtection,
    ClosePosition,
    JournalNote,
    NextEvent,
    NoArgs,
    ReducePosition,
    SetTrailingStop,
    SymbolArg,
    TradeProposal,
    UpdateExitPlan,
)
from perps_agent.calendar import TradingCalendar
from perps_agent.clock import to_utc, utc_now
from perps_agent.config.models import AppConfig
from perps_agent.execution import ExecutionEngine, ExecutionError
from perps_agent.execution.plans import ExitPlan, ExitPlanManager, profit_r
from perps_agent.journal import (
    Action,
    DecisionMade,
    Journal,
    Scenario,
    ToolCall,
    Trigger,
    TriggerKind,
    snapshot_ref,
)
from perps_agent.ledger import (
    FillBooked,
    LedgerPosition,
    OrderChanged,
    OrderPurpose,
    ProtectionChanged,
)
from perps_agent.market import MarketSnapshot
from perps_agent.risk import AutonomyLevel, RiskEngine, RiskVerdict, TradeIntent
from perps_agent.strategy import FeatureSnapshot
from perps_agent.strategy import Trigger as StrategyTrigger
from perps_agent.strategy.playbooks import (
    PLAYBOOKS_BY_ID,
    Playbook,
    Setup,
    parameter_bounds,
)
from perps_agent.venue import Instrument, OrderState, VenueError

log = logging.getLogger(__name__)

# Events kept for lookup by id (journal triggers), oldest dropped first.
_EVENT_MEMORY = 500


class ToolError(Exception):
    """A tool call that cannot be carried out; the message is shown to the agent."""


class MarketView(Protocol):
    """What the engine knows about each symbol right now."""

    def snapshot(self, symbol: str) -> MarketSnapshot: ...

    def features(self, symbol: str) -> FeatureSnapshot | None: ...

    def setups(self, symbol: str) -> tuple[Setup, ...]: ...


@dataclass(frozen=True, slots=True)
class AgentEvent:
    event_id: str
    kind: TriggerKind
    at: datetime
    key: str
    detail: str
    symbol: str | None = None
    setup: Setup | None = None

    def trigger(self) -> Trigger:
        playbook = self.setup.playbook if self.setup is not None else None
        return Trigger(kind=self.kind, detail=self.detail, playbook=playbook)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    run: Callable[[Any], Awaitable[Json]]


class AgentBridge:
    def __init__(
        self,
        *,
        config: AppConfig,
        market: MarketView,
        instruments: Sequence[Instrument],
        risk: RiskEngine,
        execution: ExecutionEngine,
        plans: ExitPlanManager,
        journal: Journal,
        calendar: TradingCalendar,
        playbooks: Sequence[Playbook[Any]],
        screened: Iterable[str] = (),
        briefing: Callable[[], str] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._market = market
        self._instruments = {i.symbol: i for i in instruments}
        self._risk = risk
        self._execution = execution
        self._plans = plans
        self._journal = journal
        self._calendar = calendar
        self._playbooks = tuple(playbooks)
        # Playbook tags (``<id>@<version>.<hash>``) that passed the Gate 1 screen (WP13).
        self._screened = frozenset(screened)
        self._briefing = briefing
        self._clock = clock
        self._zone = config.session.zone
        self._queue: deque[AgentEvent] = deque()
        self._events: dict[str, AgentEvent] = {}
        self._ids = itertools.count(1)
        self._wakeup = asyncio.Event()
        self._ledger_seen = 0
        self._reported: set[str] = set()
        self._entry_block: str | None = None
        self._halted = False
        self.last_call_at: datetime | None = None
        self.tools: dict[str, Tool] = {t.name: t for t in self._tool_list()}

    # --- Engine side ---

    def push(self, triggers: Iterable[StrategyTrigger]) -> None:
        """Queue the trigger detector's wake-ups for the agent."""
        for trigger in triggers:
            setup = trigger.setup
            if (
                setup is not None
                and not self._is_screened(setup.playbook)
                and not self._config.agent.unscreened_playbook_signals
            ):
                continue
            self.notify(
                trigger.kind,
                trigger.key,
                trigger.detail,
                symbol=trigger.symbol,
                setup=setup,
                at=trigger.at,
            )

    def notify(
        self,
        kind: TriggerKind,
        key: str,
        detail: str,
        *,
        symbol: str | None = None,
        setup: Setup | None = None,
        at: datetime | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            event_id=f"e{next(self._ids)}",
            kind=kind,
            at=to_utc(at) if at is not None else self._now(),
            key=key,
            detail=detail,
            symbol=symbol,
            setup=setup,
        )
        self._queue.append(event)
        self._events[event.event_id] = event
        while len(self._events) > _EVENT_MEMORY:
            del self._events[next(iter(self._events))]
        self._wakeup.set()
        return event

    async def sync(self) -> None:
        """Turn new ledger events and risk-state changes into agent events."""
        ledger = self._execution.ledger
        events = ledger.events
        for event in events[self._ledger_seen :]:
            match event:
                case OrderChanged():
                    self._order_event(event)
                case ProtectionChanged() if "triggered" in event.reason:
                    self.notify(
                        TriggerKind.POSITION,
                        "position:protection_triggered",
                        f"{event.symbol}: {event.reason}",
                        symbol=event.symbol,
                        at=event.at,
                    )
                case FillBooked():
                    position = ledger.position(event.position_id)
                    if position is not None and not position.is_open:
                        self._closed_event(position)
        self._ledger_seen = len(events)
        block = self._execution.entry_block
        if block is not None and block != self._entry_block:
            self.notify(TriggerKind.RISK, "risk:entry_block", f"new entries blocked: {block}")
        self._entry_block = block
        halted = self._risk.entries_halted(await self._execution.risk_state())
        if halted and not self._halted:
            self.notify(
                TriggerKind.RISK,
                "risk:daily_loss",
                "daily loss limit reached: no new entries today, exits still allowed",
            )
        self._halted = halted

    def _order_event(self, change: OrderChanged) -> None:
        order = self._execution.ledger.order(change.client_order_id)
        if order is None or not order.sent.purpose.opens or not change.status.is_terminal:
            return
        if change.client_order_id in self._reported:
            return
        self._reported.add(change.client_order_id)
        sent = order.sent
        what = "add" if sent.purpose is OrderPurpose.ADD else "entry"
        position = self._execution.ledger.position(sent.position_id)
        filled = change.filled_quantity
        if filled >= sent.quantity:
            avg = context.price(position.average_entry) if position else "?"
            detail = f"{sent.symbol}: {what} filled, {filled} @ avg {avg}"
            key = f"order:{what}_filled"
        elif filled > 0:
            detail = (
                f"{sent.symbol}: {what} partly filled, {filled} of {sent.quantity} "
                f"({change.status})"
            )
            key = f"order:{what}_partial"
        else:
            detail = f"{sent.symbol}: {what} not filled ({change.status})"
            key = f"order:{what}_unfilled"
        self.notify(TriggerKind.ORDER, key, detail, symbol=sent.symbol, at=change.at)

    def _closed_event(self, position: LedgerPosition) -> None:
        if position.position_id in self._reported:
            return
        self._reported.add(position.position_id)
        r = (
            f", {position.net_pnl / position.approved_risk:+.2f}R"
            if position.approved_risk > 0
            else ""
        )
        reason = self._exit_reason(position)
        self.notify(
            TriggerKind.ORDER,
            "order:position_closed",
            f"{position.symbol} {position.direction} closed: net {position.net_pnl:+.2f} pUSD"
            f"{r} ({reason})",
            symbol=position.symbol,
            at=position.closed_at,
        )

    def _exit_reason(self, position: LedgerPosition) -> str:
        reasons = [
            o.sent.reason
            for o in self._ledger_orders(position.position_id)
            if not o.sent.purpose.opens and o.filled_quantity > 0 and o.sent.reason
        ]
        return reasons[-1] if reasons else "exit"

    def _ledger_orders(self, position_id: str) -> list[Any]:
        ledger = self._execution.ledger
        ids = {
            e.client_order_id
            for e in ledger.events
            if isinstance(e, FillBooked) and e.position_id == position_id
        }
        orders = (ledger.order(i) for i in ids)
        return sorted((o for o in orders if o is not None), key=lambda o: o.sent.at)

    # --- Dispatch ---

    async def call(self, name: str, args: Mapping[str, Any] | None = None) -> Json:
        """Run one tool. Raises :class:`ToolError` with a message for the agent."""
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool {name!r}; tools: {', '.join(self.tools)}")
        try:
            request = tool.schema.model_validate(dict(args or {}))
        except ValidationError as error:
            raise ToolError(f"invalid arguments for {name}: {error}") from None
        self.last_call_at = self._now()
        return await tool.run(request)

    def describe_tools(self) -> list[Json]:
        return [
            {"name": t.name, "description": t.description, "input": t.schema.model_json_schema()}
            for t in self.tools.values()
        ]

    def _tool_list(self) -> list[Tool]:
        return [
            Tool(
                "next_event",
                "Wait for the next events (or the timeout) and get them with the symbols' "
                "snapshots and the session state.",
                NextEvent,
                self._next_event,
            ),
            Tool("get_briefing", "The session briefing.", NoArgs, self._get_briefing),
            Tool(
                "get_snapshot",
                "Market, features, regime, setups and position for one symbol.",
                SymbolArg,
                self._get_snapshot,
            ),
            Tool(
                "get_portfolio",
                "Equity, PnL, risk budget, limits, positions and today's trades.",
                NoArgs,
                self._get_portfolio,
            ),
            Tool(
                "list_playbooks",
                "Playbooks, their Gate 1 status, parameters and live setups.",
                NoArgs,
                self._list_playbooks,
            ),
            Tool(
                "propose_trade",
                "Propose an entry. Returns the risk verdict and, if approved, the order.",
                TradeProposal,
                self._propose_trade,
            ),
            Tool(
                "add_to_position",
                "Add to a profitable position; the stop becomes the whole position's stop.",
                AddToPosition,
                self._add_to_position,
            ),
            Tool(
                "adjust_protection",
                "Tighten the stop, or move or remove the target.",
                AdjustProtection,
                self._adjust_protection,
            ),
            Tool(
                "update_exit_plan",
                "Replace the position's exit plan (scale-outs, breakeven, trailing).",
                UpdateExitPlan,
                self._update_exit_plan,
            ),
            Tool(
                "set_trailing_stop",
                "Start, change or stop the engine-run trailing rule.",
                SetTrailingStop,
                self._set_trailing_stop,
            ),
            Tool(
                "reduce_position",
                "Exit a fraction of the position now.",
                ReducePosition,
                self._reduce_position,
            ),
            Tool("close_position", "Exit the whole position now.", ClosePosition, self._close),
            Tool(
                "journal",
                "Record reasoning not tied to an order, or a decision to stand aside.",
                JournalNote,
                self._note,
            ),
        ]

    # --- Reading ---

    async def _next_event(self, request: NextEvent) -> Json:
        timeout = request.timeout_seconds
        if timeout is None:
            timeout = self._config.agent.next_event_timeout_seconds
        if not self._queue and timeout > 0:
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout)
            except TimeoutError:
                pass
        batch: list[AgentEvent] = []
        while self._queue and len(batch) < self._config.agent.max_events_per_call:
            batch.append(self._queue.popleft())
        if not self._queue:
            self._wakeup.clear()
        symbols = list(dict.fromkeys(e.symbol for e in batch if e.symbol is not None))
        return {
            "events": [self._event_view(e) for e in batch],
            "pending": len(self._queue),
            "symbols": [self._symbol_view(s) for s in symbols],
            "session": await self._portfolio(),
        }

    def _event_view(self, event: AgentEvent) -> Json:
        view = context.drop_none(
            {
                "id": event.event_id,
                "time": context.clock(event.at, self._zone),
                "kind": event.kind,
                "key": event.key,
                "symbol": event.symbol,
                "detail": event.detail,
            }
        )
        if event.setup is not None:
            view["setup"] = self._setup_view(event.setup)
        return view

    async def _get_briefing(self, _: NoArgs) -> Json:
        text = self._briefing() if self._briefing is not None else "No briefing configured."
        return {"briefing": text}

    async def _get_snapshot(self, request: SymbolArg) -> Json:
        self._instrument(request.symbol)
        return self._symbol_view(request.symbol)

    async def _get_portfolio(self, _: NoArgs) -> Json:
        return await self._portfolio()

    async def _list_playbooks(self, _: NoArgs) -> Json:
        playbooks = []
        for playbook in self._playbooks:
            live = [
                self._setup_view(s)
                for symbol in self._instruments
                for s in self._market.setups(symbol)
                if s.playbook == playbook.tag
            ]
            playbooks.append(
                {
                    "id": playbook.id,
                    "tag": playbook.tag,
                    "screened": self._is_screened(playbook.tag),
                    "kind": playbook.kind,
                    "long_only": playbook.long_only,
                    "description": playbook.description,
                    "parameters": {
                        b.name: {
                            "value": str(getattr(playbook.params, b.name)),
                            "min": str(b.minimum),
                            "max": str(b.maximum),
                        }
                        for b in parameter_bounds(playbook.params_type)
                    },
                    "live_setups": live,
                }
            )
        return {
            "gate1": (
                "Playbooks trade (L0/L1) only once their version passes the Gate 1 backtest "
                "screen. Unscreened setups are context: act on one only as a discretionary "
                "(L2) trade."
            ),
            "playbooks": playbooks,
        }

    def _symbol_view(self, symbol: str) -> Json:
        position = self._execution.ledger.open_position(symbol)
        return context.symbol_view(
            symbol,
            self._market.snapshot(symbol),
            self._market.features(symbol),
            [self._setup_view(s) for s in self._visible_setups(symbol)],
            self._position_view(position) if position is not None else None,
        )

    def _visible_setups(self, symbol: str) -> list[Setup]:
        return [
            s
            for s in self._market.setups(symbol)
            if self._is_screened(s.playbook) or self._config.agent.unscreened_playbook_signals
        ]

    def _setup_view(self, setup: Setup) -> Json:
        return context.setup_view(setup, screened=self._is_screened(setup.playbook))

    async def _portfolio(self) -> Json:
        now = self._now()
        state = await self._execution.risk_state()
        window = self._calendar.window_at(now)
        risk = self._config.risk
        ledger = self._execution.ledger
        view: Json = {
            "time": context.clock(now, self._zone),
            "entries_allowed": self._calendar.is_entry_allowed(now),
        }
        if window is not None:
            view["minutes_to_entry_cutoff"] = max(0, _minutes(window.entry_cutoff - now))
            view["minutes_to_window_end"] = max(0, _minutes(window.end - now))
        view.update(
            {
                "equity": context.price(state.equity),
                "day_start_equity": context.price(state.day_start_equity),
                "day_pnl": f"{state.day_pnl:+.2f}",
                "daily_loss_room": f"{max(Decimal(0), self._risk.daily_loss_room(state)):.2f}",
                "entries_halted": self._risk.entries_halted(state),
                "risk_budget_full_size": {
                    "L0_L1": f"{self._risk.risk_budget(state, AutonomyLevel.L1):.2f}",
                    "L2": f"{self._risk.risk_budget(state, AutonomyLevel.L2):.2f}",
                },
                "trades_today": f"{state.trades_today}/{risk.max_trades_per_day}",
                "l2_trades_today": f"{state.l2_trades_today}/{risk.max_l2_trades_per_day}",
                "positions": [self._position_view(p) for p in ledger.open_positions],
                "working_entries": [
                    {
                        "symbol": o.sent.symbol,
                        "side": str(o.sent.side),
                        "quantity": str(o.sent.quantity),
                        "price": context.price(o.sent.price),
                        "filled": str(o.filled_quantity),
                    }
                    for o in ledger.live_orders(purposes=(OrderPurpose.ENTRY, OrderPurpose.ADD))
                ],
                "closed_today": [
                    {
                        "symbol": p.symbol,
                        "side": p.direction,
                        "net_pnl": f"{p.net_pnl:+.2f}",
                        "r": (
                            f"{p.net_pnl / p.approved_risk:+.2f}" if p.approved_risk > 0 else None
                        ),
                        "source": _source(p),
                    }
                    for p in ledger.positions
                    if not p.is_open
                ],
            }
        )
        block = self._execution.entry_block
        if block is not None:
            view["entry_block"] = block
        return view

    def _position_view(self, position: LedgerPosition) -> Json:
        snapshot = self._market.snapshot(position.symbol)
        mark = snapshot.mark_price or position.average_entry
        stop = position.planned_stop or position.stop_price
        r_unit = abs(position.average_entry - stop) if stop is not None else Decimal(0)
        plan = self._plans.plan(position.symbol)
        return context.drop_none(
            {
                "side": position.direction,
                "quantity": str(position.quantity),
                "entry": context.price(position.average_entry),
                "mark": context.price(mark),
                "stop": context.price(position.stop_price),
                "target": context.price(position.target_price),
                "unrealized_pnl": f"{position.unrealized_pnl(mark):+.2f}",
                "r_now": f"{profit_r(position, mark, r_unit):+.2f}" if r_unit > 0 else None,
                "approved_risk": f"{position.approved_risk:.2f}",
                "source": _source(position),
                "time_stop": (
                    context.clock(position.time_stop, self._zone) if position.time_stop else None
                ),
                "exit_plan": plan.describe() if plan is not None else None,
                "closing": self._execution.closing.get(position.symbol),
                "opened": context.clock(position.opened_at, self._zone),
            }
        )

    # --- Entries ---

    async def _propose_trade(self, p: TradeProposal) -> Json:
        instrument = self._instrument(p.symbol)
        level, tag = self._resolve_source(p)
        now = self._now()
        time_stop = None
        if p.hold_minutes is not None:
            time_stop = now + timedelta(minutes=p.hold_minutes)
            window = self._calendar.window_at(now)
            if window is not None:
                time_stop = min(time_stop, window.end)
        intent = TradeIntent(
            symbol=p.symbol,
            direction=p.side,
            level=level,
            playbook=tag,
            entry_price=p.entry_price,
            post_only=p.post_only,
            size=p.size,
            stop_price=p.stop_price,
            target_price=p.target_price,
            time_stop=time_stop,
            thesis=p.thesis,
            confidence=p.confidence,
        )
        snapshot, features = self._market.snapshot(p.symbol), self._market.features(p.symbol)
        decision = self._decide(
            Action.ENTER,
            p.event_id,
            symbol=p.symbol,
            snapshot=snapshot,
            features=features,
            regime=p.regime,
            setup=p.setup or (p.playbook_id or "discretionary"),
            thesis=p.thesis,
            scenarios=p.scenarios,
            chosen_scenario=p.chosen_scenario,
            expected_r=p.expected_r,
            intent=intent,
            tool_calls=p.tool_calls,
            request=f"exit plan: {p.exit_plan.describe()}" if p.exit_plan else None,
        )
        return await self._evaluate_and_send(
            decision, intent, instrument, snapshot, features, adding=False, plan=p.exit_plan
        )

    async def _add_to_position(self, p: AddToPosition) -> Json:
        instrument = self._instrument(p.symbol)
        position = self._execution.ledger.open_position(p.symbol)
        if position is None:
            raise ToolError(f"no open position in {p.symbol}")
        level = position.level or AutonomyLevel.L2
        intent = TradeIntent(
            symbol=p.symbol,
            direction=position.direction,
            level=level,
            playbook=None if level is AutonomyLevel.L2 else position.playbook,
            entry_price=p.entry_price,
            post_only=p.post_only,
            size=p.size,
            stop_price=p.stop_price,
            target_price=p.target_price if p.target_price is not None else position.target_price,
            time_stop=position.time_stop,
            thesis=p.thesis,
            confidence=p.confidence,
        )
        snapshot, features = self._market.snapshot(p.symbol), self._market.features(p.symbol)
        decision = self._decide(
            Action.ADD,
            p.event_id,
            symbol=p.symbol,
            snapshot=snapshot,
            features=features,
            regime=p.regime,
            setup=p.setup or _setup_key(position),
            thesis=p.thesis,
            scenarios=p.scenarios,
            chosen_scenario=p.chosen_scenario,
            expected_r=p.expected_r,
            intent=intent,
            tool_calls=p.tool_calls,
        )
        return await self._evaluate_and_send(
            decision, intent, instrument, snapshot, features, adding=True, plan=None
        )

    def _resolve_source(self, p: TradeProposal) -> tuple[AutonomyLevel, str | None]:
        playbook_id = p.playbook_id
        if playbook_id is None:
            return AutonomyLevel.L2, None
        if playbook_id not in PLAYBOOKS_BY_ID:
            raise ToolError(
                f"unknown playbook {playbook_id!r}; known: {', '.join(PLAYBOOKS_BY_ID)}"
            )
        setups = [
            s
            for s in self._market.setups(p.symbol)
            if s.playbook.split("@", 1)[0] == playbook_id and s.direction is p.side
        ]
        if not setups:
            raise ToolError(
                f"{playbook_id} has no live {p.side} setup on {p.symbol}; "
                "propose it as discretionary if you still want the trade"
            )
        setup = setups[0]
        if not self._is_screened(setup.playbook):
            raise ToolError(
                f"{setup.playbook} has not passed the Gate 1 backtest screen, so it cannot "
                "trade as a playbook yet; propose the idea as discretionary (L2) instead"
            )
        if p.size > setup.max_size:
            raise ToolError(f"size {p.size} exceeds the setup's cap {setup.max_size}")
        as_is = (
            p.entry_price == setup.entry_price
            and p.stop_price == setup.stop_price
            and p.target_price == setup.target_price
        )
        return (AutonomyLevel.L0 if as_is else AutonomyLevel.L1), setup.playbook

    async def _evaluate_and_send(
        self,
        decision: DecisionMade,
        intent: TradeIntent,
        instrument: Instrument,
        snapshot: MarketSnapshot,
        features: FeatureSnapshot | None,
        *,
        adding: bool,
        plan: ExitPlan | None,
    ) -> Json:
        state = await self._execution.risk_state()
        evaluate = self._risk.evaluate_add if adding else self._risk.evaluate
        verdict = evaluate(
            intent,
            instrument=instrument,
            snapshot=snapshot,
            state=state,
            range_15m=features.volatility.range_15m if features is not None else None,
            now=self._now(),
        )
        self._journal.record_verdict(decision.decision_id, verdict)
        result = {"decision_id": decision.decision_id, **_verdict_view(verdict)}
        if not verdict.approved:
            return result
        try:
            order = await self._execution.enter(verdict)
        except (ExecutionError, VenueError) as error:
            self._journal.record_failure(decision.decision_id, str(error))
            result["error"] = str(error)
            return result
        self._journal.link_order(
            decision.decision_id, order.request.client_order_id, self._execution.ledger
        )
        if plan is not None:
            self._plans.set_plan(intent.symbol, plan)
        result["order"] = _order_view(order)
        return result

    # --- Management ---

    async def _adjust_protection(self, p: AdjustProtection) -> Json:
        parts = []
        if p.stop_price is not None:
            parts.append(f"stop to {p.stop_price}")
        if p.target_price is not None:
            parts.append(f"target to {p.target_price}")
        if p.remove_target:
            parts.append("remove target")
        decision = self._manage(Action.ADJUST, p.symbol, p.event_id, p.reason, ", ".join(parts))
        try:
            refused = await self._execution.update_protection(
                p.symbol,
                stop_price=p.stop_price,
                target_price=p.target_price,
                remove_target=p.remove_target,
                reason=p.reason,
            )
        except (ExecutionError, VenueError) as error:
            return self._failed(decision, str(error))
        if refused is not None:
            return self._failed(decision, f"{refused.check}: {refused.detail}")
        return {"done": True, "position": self._position_or_flat(p.symbol)}

    async def _update_exit_plan(self, p: UpdateExitPlan) -> Json:
        self._require_position(p.symbol)
        detail = p.plan.describe() if p.plan is not None else "remove exit plan"
        self._manage(Action.ADJUST, p.symbol, p.event_id, p.reason, f"exit plan: {detail}")
        self._plans.set_plan(p.symbol, p.plan)
        return {"done": True, "exit_plan": detail}

    async def _set_trailing_stop(self, p: SetTrailingStop) -> Json:
        self._require_position(p.symbol)
        current = self._plans.plan(p.symbol) or ExitPlan()
        plan = current.model_copy(update={"trailing": p.rule})
        detail = p.rule.describe() if p.rule is not None else "stop trailing"
        self._manage(Action.ADJUST, p.symbol, p.event_id, p.reason, f"trailing: {detail}")
        self._plans.set_plan(p.symbol, plan)
        return {"done": True, "exit_plan": plan.describe()}

    async def _reduce_position(self, p: ReducePosition) -> Json:
        decision = self._manage(
            Action.REDUCE, p.symbol, p.event_id, p.reason, f"reduce by {p.fraction}"
        )
        try:
            order = await self._execution.reduce_position(p.symbol, p.fraction, p.reason)
        except (ExecutionError, VenueError) as error:
            return self._failed(decision, str(error))
        self._journal.link_order(
            decision.decision_id, order.request.client_order_id, self._execution.ledger
        )
        return {"done": True, "order": _order_view(order)}

    async def _close(self, p: ClosePosition) -> Json:
        decision = self._manage(Action.CLOSE, p.symbol, p.event_id, p.reason, "close")
        try:
            order = await self._execution.close_position(p.symbol, p.reason)
        except (ExecutionError, VenueError) as error:
            return self._failed(decision, str(error))
        if order is None:
            return {"done": True, "detail": "flat, or an exit is already working"}
        self._journal.link_order(
            decision.decision_id, order.request.client_order_id, self._execution.ledger
        )
        return {"done": True, "order": _order_view(order)}

    async def _note(self, p: JournalNote) -> Json:
        snapshot = features = None
        if p.symbol is not None:
            self._instrument(p.symbol)
            snapshot, features = self._market.snapshot(p.symbol), self._market.features(p.symbol)
        decision = self._decide(
            Action.STAND_ASIDE if p.stand_aside else Action.NOTE,
            p.event_id,
            symbol=p.symbol,
            snapshot=snapshot,
            features=features,
            regime=p.regime,
            setup=p.setup,
            thesis=p.note,
            scenarios=p.scenarios,
        )
        return {"recorded": decision.decision_id}

    def _manage(
        self, action: Action, symbol: str, event_id: str | None, reason: str, request: str
    ) -> DecisionMade:
        self._require_position(symbol)
        return self._decide(
            action,
            event_id,
            symbol=symbol,
            snapshot=self._market.snapshot(symbol),
            features=self._market.features(symbol),
            thesis=reason,
            request=request,
        )

    def _failed(self, decision: DecisionMade, error: str) -> Json:
        self._journal.record_failure(decision.decision_id, error)
        return {"done": False, "error": error}

    # --- Journal ---

    def _decide(
        self,
        action: Action,
        event_id: str | None,
        *,
        symbol: str | None,
        snapshot: MarketSnapshot | None,
        features: FeatureSnapshot | None,
        thesis: str,
        regime: str | None = None,
        setup: str | None = None,
        scenarios: tuple[Scenario, ...] = (),
        chosen_scenario: str | None = None,
        expected_r: Decimal | None = None,
        intent: TradeIntent | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
        request: str | None = None,
    ) -> DecisionMade:
        event = self._events.get(event_id) if event_id is not None else None
        if event_id is not None and event is None:
            raise ToolError(f"unknown event {event_id!r}")
        trigger = (
            event.trigger()
            if event is not None
            else Trigger(kind=TriggerKind.AGENT, detail="agent initiative")
        )
        if regime is None and features is not None and features.regime.labels:
            regime = "_".join(features.regime.labels)
        return self._journal.decide(
            trigger=trigger,
            action=action,
            symbol=symbol,
            snapshot=(
                snapshot_ref(snapshot, _features_used(snapshot, features))
                if snapshot is not None
                else None
            ),
            regime=regime,
            setup=setup,
            thesis=thesis,
            scenarios=scenarios,
            chosen_scenario=chosen_scenario,
            expected_r=expected_r,
            intent=intent,
            request=request,
            tool_calls=tool_calls,
        )

    # --- Helpers ---

    def _is_screened(self, tag: str) -> bool:
        return tag in self._screened

    def _instrument(self, symbol: str) -> Instrument:
        try:
            return self._instruments[symbol]
        except KeyError:
            raise ToolError(
                f"{symbol!r} is not traded; symbols: {', '.join(self._instruments)}"
            ) from None

    def _require_position(self, symbol: str) -> LedgerPosition:
        self._instrument(symbol)
        position = self._execution.ledger.open_position(symbol)
        if position is None:
            raise ToolError(f"no open position in {symbol}")
        return position

    def _position_or_flat(self, symbol: str) -> Json | None:
        position = self._execution.ledger.open_position(symbol)
        return self._position_view(position) if position is not None else None

    def _now(self) -> datetime:
        return to_utc(self._clock())


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _source(position: LedgerPosition) -> str:
    if position.level is None:
        return "venue"
    if position.playbook is None:
        return f"{position.level} discretionary"
    return f"{position.level} {position.playbook}"


def _setup_key(position: LedgerPosition) -> str:
    return position.playbook.split("@", 1)[0] if position.playbook else "discretionary"


def _verdict_view(verdict: RiskVerdict) -> Json:
    order = verdict.order
    return context.drop_none(
        {
            "decision": verdict.decision,
            "reasons": [f"{r.check}: {r.detail}" for r in verdict.reasons],
            "level": verdict.intent.level,
            "playbook": verdict.intent.playbook,
            "quantity": str(order.quantity) if order is not None else None,
            "entry": context.price(verdict.entry_price),
            "stop": context.price(verdict.stop_price),
            "target": context.price(verdict.target_price),
            "leverage": verdict.leverage,
            "risk_budget": _money(verdict.risk_budget),
            "risk_at_stop": _money(verdict.risk_amount),
            "time_in_force": order.time_in_force if order is not None else None,
        }
    )


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _order_view(order: OrderState) -> Json:
    return {
        "status": order.status,
        "filled": str(order.filled_quantity),
        "resting": str(order.resting_quantity),
    }


def _features_used(
    snapshot: MarketSnapshot, features: FeatureSnapshot | None
) -> dict[str, Decimal]:
    """The numbers a decision is filed with in the journal, by stable name."""
    used: dict[str, Decimal | None] = {"spread_bp": snapshot.spread_bp}
    if features is not None:
        vol = features.volatility
        used.update(
            range_15m_bp=vol.range_15m_bp,
            range_60m_bp=vol.range_60m_bp,
            minute_vol_bp=vol.minute_vol_bp,
            expected_move_to_close_bp=vol.expected_move_to_close_bp,
            range_ratio=features.regime.range_ratio,
        )
        if features.session is not None:
            used.update(
                vs_open_bp=features.session.price_vs_open_bp,
                vs_vwap_bp=features.session.price_vs_vwap_bp,
                relative_volume=features.session.relative_volume,
            )
        if features.profile is not None:
            poc = features.profile.poc
            used["vs_poc_bp"] = (features.price - poc) / poc * 10_000
    return {k: v for k, v in used.items() if v is not None}
