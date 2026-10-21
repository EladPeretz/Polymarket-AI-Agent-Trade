"""Trigger detector: turns market activity and the clock into agent wake-ups (overview §3.4).

* **Playbook:** a playbook's setup appeared (edge-triggered: it fires again
  only after the setup has gone away).
* **Large move:** price moved at least ``large_move_sigma`` × the 1-minute
  volatility scaled to ``large_move_seconds``, and at least
  ``large_move_min_bp``, within ``large_move_seconds``. After one fires, the
  symbol's next large moves are held back for ``large_move_cooldown_seconds``
  unless price has gone ``large_move_escalation`` × the threshold beyond where
  the last one fired; the next one that fires says how many were held back.
* **Position:** approaching the stop or the target, or the time stop is near;
  each once per stop, target or time stop.
* **Check-in:** every ``check_in_minutes`` from the window start.
* **Session:** window open, entry cutoff, window close.

Fills, rejections and risk events come from execution and the risk engine;
the agent bridge (WP10) forwards them with these.

The detector is fed explicitly (features, positions, the time), with no I/O,
so backtests and tests drive it the same way the live loop does.
"""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from perps_agent.calendar import TradingCalendar
from perps_agent.clock import to_utc
from perps_agent.config.models import TriggersConfig
from perps_agent.journal import TriggerKind
from perps_agent.risk import Direction
from perps_agent.strategy.features import bp
from perps_agent.strategy.playbooks import Setup
from perps_agent.strategy.snapshot import FeatureSnapshot


@dataclass(frozen=True, slots=True)
class Trigger:
    kind: TriggerKind
    at: datetime
    # A stable name for what fired, e.g. "session:open" or "playbook:vwap_reversion:short".
    key: str
    detail: str
    symbol: str | None = None
    setup: Setup | None = None


@dataclass(frozen=True, slots=True)
class WatchedPosition:
    """An open position as the detector watches it."""

    symbol: str
    direction: Direction
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None = None
    time_stop: datetime | None = None


@dataclass(slots=True)
class _SymbolState:
    active_setups: set[str] = field(default_factory=set)
    prices: deque[tuple[datetime, Decimal]] = field(default_factory=deque)
    # Position events already sent, by key; cleared when the position closes.
    position_events: set[str] = field(default_factory=set)
    # The last large move that fired, and how many were held back since.
    last_move: tuple[datetime, Decimal] | None = None
    held_moves: int = 0


class TriggerDetector:
    def __init__(
        self, calendar: TradingCalendar, config: TriggersConfig, *, check_in_minutes: int
    ) -> None:
        self._calendar = calendar
        self._config = config
        self._check_in = timedelta(minutes=check_in_minutes)
        self._symbols: dict[str, _SymbolState] = {}
        self._day: date | None = None
        self._sent_milestones: set[str] = set()
        self._last_check_in: datetime | None = None

    # --- Market ---

    def on_features(
        self,
        features: FeatureSnapshot,
        setups: Iterable[Setup],
        position: WatchedPosition | None = None,
    ) -> list[Trigger]:
        """Triggers for one symbol's new snapshot, its current setups and its position."""
        state = self._symbols.setdefault(features.symbol, _SymbolState())
        triggers = self._setup_triggers(features, setups, state)
        move = self._large_move(features, state)
        if move is not None:
            triggers.append(move)
        triggers += self._position_triggers(features, position, state)
        return triggers

    def _setup_triggers(
        self, features: FeatureSnapshot, setups: Iterable[Setup], state: _SymbolState
    ) -> list[Trigger]:
        current: dict[str, Setup] = {}
        for setup in setups:
            playbook_id = setup.playbook.split("@", 1)[0]
            current[f"playbook:{playbook_id}:{setup.direction}"] = setup
        triggers = [
            Trigger(
                kind=TriggerKind.PLAYBOOK,
                at=features.at,
                key=key,
                detail=f"{setup.playbook} {setup.direction}: " + "; ".join(setup.reasons),
                symbol=features.symbol,
                setup=setup,
            )
            for key, setup in current.items()
            if key not in state.active_setups
        ]
        state.active_setups = set(current)
        return triggers

    def _large_move(self, features: FeatureSnapshot, state: _SymbolState) -> Trigger | None:
        config = self._config
        span = timedelta(seconds=config.large_move_seconds)
        prices = state.prices
        prices.append((features.at, features.price))
        while prices and prices[0][0] < features.at - span:
            prices.popleft()
        oldest_at, oldest = prices[0]
        if oldest_at == features.at:
            return None
        move_bp = bp(features.price - oldest, oldest)
        threshold = config.large_move_min_bp
        vol = features.volatility.minute_vol_bp
        if vol is not None:
            scaled = vol * (Decimal(config.large_move_seconds) / 60).sqrt()
            threshold = max(threshold, config.large_move_sigma * scaled)
        elif features.volatility.range_15m_bp is not None:
            range_floor = config.large_move_range_fraction * features.volatility.range_15m_bp
            threshold = max(threshold, range_floor)
        if abs(move_bp) < threshold or threshold <= 0:
            return None
        # Start over, so one move wakes the agent once.
        prices.clear()
        prices.append((features.at, features.price))
        cooldown = timedelta(seconds=config.large_move_cooldown_seconds)
        if state.last_move is not None and features.at - state.last_move[0] < cooldown:
            since_last = bp(features.price - state.last_move[1], state.last_move[1])
            if abs(since_last) < config.large_move_escalation * threshold:
                state.held_moves += 1
                return None
        seconds = int((features.at - oldest_at).total_seconds())
        detail = f"{move_bp:+.1f} bp in {seconds}s (threshold {threshold:.1f} bp)"
        if state.held_moves:
            detail += f"; {state.held_moves} smaller moves held back since the last one"
        state.last_move, state.held_moves = (features.at, features.price), 0
        return Trigger(
            kind=TriggerKind.LARGE_MOVE,
            at=features.at,
            key=f"large_move:{'up' if move_bp > 0 else 'down'}",
            detail=detail,
            symbol=features.symbol,
        )

    def _position_triggers(
        self, features: FeatureSnapshot, position: WatchedPosition | None, state: _SymbolState
    ) -> list[Trigger]:
        if position is None:
            state.position_events.clear()
            return []
        config = self._config
        sign = position.direction.sign
        price, now = features.price, features.at
        found: list[tuple[str, str]] = []
        stop, target = position.stop_price, position.target_price
        # Measured against the entry-to-stop distance, also once the stop is past entry.
        span = abs(position.entry_price - stop)
        if span > 0 and (price - stop) * sign <= config.approaching_stop_fraction * span:
            found.append((f"stop:{stop}", f"price {price} near stop {stop}"))
        if target is not None:
            reward = (target - position.entry_price) * sign
            progress = (price - position.entry_price) * sign
            if reward > 0 and progress >= config.approaching_target_fraction * reward:
                found.append((f"target:{target}", f"price {price} near target {target}"))
        time_stop = position.time_stop
        if time_stop is not None:
            if now >= time_stop - timedelta(minutes=config.time_stop_warning_minutes):
                found.append(
                    (f"time_stop:{time_stop.isoformat()}", f"time stop at {time_stop:%H:%M} UTC")
                )
        triggers = []
        for key, detail in found:
            if key in state.position_events:
                continue
            state.position_events.add(key)
            triggers.append(
                Trigger(
                    kind=TriggerKind.POSITION,
                    at=now,
                    key=f"position:{key.split(':', 1)[0]}",
                    detail=detail,
                    symbol=position.symbol,
                )
            )
        return triggers

    # --- Clock ---

    def on_clock(self, now: datetime) -> list[Trigger]:
        """Session milestones and the scheduled check-in due at ``now``.

        Called late, it sends each missed milestone once and at most one check-in.
        """
        now = to_utc(now)
        window = self._calendar.window_at(now)
        if window is None:
            return []
        if window.date != self._day:
            self._day = window.date
            self._sent_milestones = set()
            self._last_check_in = None
        triggers = []
        milestones = (
            ("session:open", window.start, "trading window open"),
            ("session:entry_cutoff", window.entry_cutoff, "entry cutoff: exits only from now"),
            ("session:close", window.end, "window close: everything must be flat"),
        )
        for key, at, detail in milestones:
            if now >= at and key not in self._sent_milestones:
                self._sent_milestones.add(key)
                triggers.append(Trigger(kind=TriggerKind.SESSION, at=now, key=key, detail=detail))
        if window.start < now < window.end:
            due = window.start + ((now - window.start) // self._check_in) * self._check_in
            if due > window.start and (self._last_check_in is None or due > self._last_check_in):
                self._last_check_in = due
                triggers.append(
                    Trigger(
                        kind=TriggerKind.CHECK_IN,
                        at=now,
                        key="check_in",
                        detail=f"scheduled check-in ({due:%H:%M} UTC)",
                    )
                )
        return triggers
