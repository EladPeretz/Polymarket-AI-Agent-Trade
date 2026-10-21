"""The paper-trading session engine: everything between the market feed and the agent.

``SessionEngine`` is fed explicitly, so tests and replays drive it the same way
the live loop does:

* :meth:`SessionEngine.on_market` / :meth:`SessionEngine.on_rest`: each market
  event goes to the paper simulator (fills are booked by execution), the bar
  builder and the marks.
* :meth:`SessionEngine.step`: features for every symbol, playbook setups, the
  trigger detector, the exit plans, execution timers and the bridge's ledger
  events.

The window close-out, kill switch and liveness checks are the guardian's
(:mod:`perps_agent.guardian`), which the session loop runs after every step,
separately, so a failing step never stops it.

It is also the bridge's :class:`~perps_agent.agent.MarketView`.

:func:`run_paper_session` wires it to the live feed and serves the bridge on
the agent socket for one trading day. At the end it records the trade outcomes
and writes the daily report (:mod:`perps_agent.report`) into ``results/<date>/``;
``perps-agent publish`` then commits that directory to the results branch.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from perps_agent.agent import AgentBridge, BridgeServer, build_briefing, recent_activity
from perps_agent.agent.latency import AGENT_EVENTS_FILE, JsonlLatencySink
from perps_agent.calendar import TradingCalendar, TradingWindow
from perps_agent.clock import to_utc, utc_now
from perps_agent.config.models import AppConfig, Mode
from perps_agent.execution import ExecutionEngine, SimBackend
from perps_agent.execution.plans import ExitPlanManager
from perps_agent.guardian import INCIDENTS_FILE, Guardian, IncidentLog, JsonlIncidentSink
from perps_agent.journal import Journal, JsonlJournalSink, TriggerKind, journal_path
from perps_agent.ledger import LEDGER_FILE, JsonlLedgerSink, Ledger
from perps_agent.market import MarketFeed, MarketSnapshot, Recorder
from perps_agent.report import EQUITY_FILE, EquitySample, JsonlEquitySink, write_report
from perps_agent.risk import FeeRates, RiskEngine
from perps_agent.sim import PaperSimulator
from perps_agent.strategy import (
    FeatureSnapshot,
    MinuteBars,
    SessionSummary,
    TriggerDetector,
    WatchedPosition,
    build_features,
    load_history,
)
from perps_agent.strategy.playbooks import Playbook, PlaybookContext, Setup, evaluate_all
from perps_agent.strategy.snapshot import LOOKBACK
from perps_agent.venue import (
    Instrument,
    InstrumentRegistry,
    MarketDataSource,
    OrderBook,
    Ticker,
    TickerEvent,
    Trade,
    TradesEvent,
    create_market_data,
)
from perps_agent.venue.models import MarketEvent

log = logging.getLogger(__name__)

# Market events are applied this often; features and triggers every step.
PUMP_INTERVAL = timedelta(milliseconds=100)
STEP_INTERVAL = timedelta(seconds=1)


class SessionEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        instruments: Sequence[Instrument],
        calendar: TradingCalendar,
        simulator: PaperSimulator,
        execution: ExecutionEngine,
        plans: ExitPlanManager,
        playbooks: Sequence[Playbook[Any]],
        fees: FeeRates,
        snapshot: Callable[[str], MarketSnapshot],
        history: Mapping[str, Sequence[SessionSummary]],
        bars: Mapping[str, MinuteBars],
    ) -> None:
        self._config = config
        self._by_id = {i.instrument_id: i.symbol for i in instruments}
        self.symbols = tuple(i.symbol for i in instruments)
        self._calendar = calendar
        self._sim = simulator
        self._execution = execution
        self._plans = plans
        self._playbooks = tuple(playbooks)
        self._fees = fees
        self._snapshot = snapshot
        self._history = {s: tuple(history.get(s, ())) for s in self.symbols}
        self._bars = dict(bars)
        self._detector = TriggerDetector(
            calendar, config.triggers, check_in_minutes=config.agent.check_in_minutes
        )
        self._features: dict[str, FeatureSnapshot] = {}
        self._setups: dict[str, tuple[Setup, ...]] = {}
        self._pending: list[MarketEvent | tuple[OrderBook, Ticker, Sequence[Trade]]] = []
        self.bridge: AgentBridge | None = None

    # --- MarketView ---

    def snapshot(self, symbol: str) -> MarketSnapshot:
        return self._snapshot(symbol)

    def features(self, symbol: str) -> FeatureSnapshot | None:
        return self._features.get(symbol)

    def setups(self, symbol: str) -> tuple[Setup, ...]:
        return self._setups.get(symbol, ())

    # --- Market input ---

    def queue_event(self, event: MarketEvent) -> None:
        """Feed listener: applied on the next :meth:`pump`."""
        self._pending.append(event)

    def queue_rest(self, book: OrderBook, ticker: Ticker, trades: Sequence[Trade]) -> None:
        self._pending.append((book, ticker, trades))

    async def pump(self) -> None:
        pending, self._pending = self._pending, []
        for item in pending:
            if isinstance(item, tuple):
                await self.on_rest(*item)
            else:
                await self.on_market(item)

    async def on_market(self, event: MarketEvent) -> None:
        await self._execution.on_events(self._sim.on_event(event))
        match event:
            case TradesEvent() if event.trades:
                symbol = self._by_id.get(event.trades[0].instrument_id)
                if symbol is not None:
                    self._bars[symbol].add_trades(event.trades)
            case TickerEvent():
                self._mark(event.ticker)

    async def on_rest(self, book: OrderBook, ticker: Ticker, trades: Sequence[Trade]) -> None:
        await self._execution.on_events(self._sim.on_rest(book, ticker, trades))
        symbol = self._by_id.get(ticker.instrument_id)
        if symbol is not None:
            self._bars[symbol].add_trades(trades)
        self._mark(ticker)

    def _mark(self, ticker: Ticker) -> None:
        symbol = self._by_id.get(ticker.instrument_id)
        if symbol is not None and ticker.mark_price is not None:
            self._execution.update_marks({symbol: ticker.mark_price})
            self._bars[symbol].add_mark(ticker.mark_price, ticker.timestamp)

    # --- Clock ---

    async def step(self, now: datetime) -> None:
        now = to_utc(now)
        window = self._calendar.window_at(now)
        bridge = self.bridge
        for symbol in self.symbols:
            features = self._compute(symbol, now, window)
            if features is None:
                continue
            setups: tuple[Setup, ...] = ()
            if window is not None:
                context = PlaybookContext(window=window, risk=self._config.risk, fees=self._fees)
                setups = evaluate_all(self._playbooks, features, context)
            self._setups[symbol] = setups
            triggers = self._detector.on_features(features, setups, self._watched(symbol))
            done = await self._plans.on_features(features)
            if bridge is not None:
                bridge.push(triggers)
                for action in done:
                    bridge.notify(
                        TriggerKind.POSITION,
                        "position:exit_plan",
                        f"{symbol}: {action}",
                        symbol=symbol,
                    )
        if bridge is not None:
            bridge.push(self._detector.on_clock(now))
        await self._execution.tick(now)
        await self._execution.on_events(self._sim.drain())
        for bars in self._bars.values():
            bars.prune(now)
        if bridge is not None:
            await bridge.sync()

    def _compute(
        self, symbol: str, now: datetime, window: TradingWindow | None
    ) -> FeatureSnapshot | None:
        snapshot = self._snapshot(symbol)
        bars = self._bars[symbol].bars(start=_bars_from(now, window))
        price = snapshot.mark_price or (bars[-1].close if bars else None)
        if price is None:
            return None
        features = build_features(
            symbol,
            bars=bars,
            price=price,
            now=now,
            window=window,
            history=self._history[symbol],
            config=self._config.features,
        )
        self._features[symbol] = features
        return features

    def _watched(self, symbol: str) -> WatchedPosition | None:
        position = self._execution.ledger.open_position(symbol)
        if position is None:
            return None
        stop = position.stop_price or position.planned_stop
        if stop is None:
            return None
        return WatchedPosition(
            symbol=symbol,
            direction=position.direction,
            entry_price=position.average_entry,
            stop_price=stop,
            target_price=position.target_price,
            time_stop=position.time_stop,
        )

    @property
    def flat(self) -> bool:
        ledger = self._execution.ledger
        return not ledger.open_positions and not ledger.live_orders()


def _bars_from(now: datetime, window: TradingWindow | None) -> datetime:
    start = now - LOOKBACK - timedelta(minutes=5)
    if window is not None:
        start = min(start, window.market.open - LOOKBACK)
    return start


# --- Live paper session ---


@dataclass(frozen=True, slots=True)
class SessionPaths:
    results: Path
    ledger: Path
    journal: Path
    socket: Path

    @property
    def day_dir(self) -> Path:
        return self.journal.parent

    @property
    def incidents(self) -> Path:
        return self.day_dir / INCIDENTS_FILE

    @property
    def equity(self) -> Path:
        return self.day_dir / EQUITY_FILE

    @property
    def agent_events(self) -> Path:
        return self.day_dir / AGENT_EVENTS_FILE


def session_paths(config: AppConfig, day: date) -> SessionPaths:
    results = Path(config.agent.results_dir)
    journal = journal_path(results, day)
    return SessionPaths(
        results=results,
        ledger=journal.parent / LEDGER_FILE,
        journal=journal,
        socket=Path(config.agent.socket_path),
    )


class EquitySampler:
    """Writes the session's equity every ``interval`` for the daily report's drawdown."""

    def __init__(
        self,
        execution: ExecutionEngine,
        sink: Callable[[EquitySample], None],
        interval: timedelta,
    ) -> None:
        self._execution = execution
        self._sink = sink
        self._interval = interval
        self._next: datetime | None = None

    async def sample(self, now: datetime, *, force: bool = False) -> None:
        if not force and self._next is not None and now < self._next:
            return
        self._next = now + self._interval
        state = await self._execution.risk_state()
        self._sink(
            EquitySample(
                at=now,
                equity=state.equity,
                open_positions=len(self._execution.ledger.open_positions),
            )
        )


def briefing_for(
    config: AppConfig,
    calendar: TradingCalendar,
    day: date,
    playbooks: Sequence[Playbook[Any]],
    screened: Sequence[str] = (),
) -> str:
    return build_briefing(
        config,
        day=day,
        window=calendar.trading_window(day),
        playbooks=playbooks,
        screened=screened,
        activity=recent_activity(Path(config.agent.results_dir), calendar.exchange, day),
    )


async def run_paper_session(
    config: AppConfig,
    *,
    playbooks: Sequence[Playbook[Any]],
    secrets: Sequence[str] = (),
    stop: asyncio.Event | None = None,
    calendar: TradingCalendar | None = None,
) -> int:
    """Run today's paper session until the window has closed and the book is flat.

    ``calendar`` replaces the NYSE trading calendar, for rehearsals
    (:mod:`perps_agent.app.rehearsal`).
    """
    if config.mode is not Mode.PAPER:
        raise ValueError(f"run_paper_session runs paper mode only, not {config.mode}")
    stop = stop or asyncio.Event()
    calendar = calendar or TradingCalendar(config.session)
    now = utc_now()
    window = calendar.window_at(now)
    if window is None:
        day = now.astimezone(config.session.zone).date()
        log.info("not a trading day", extra={"day": day.isoformat()})
        return 3
    day = window.date
    paths = session_paths(config, day)
    if paths.ledger.exists():
        raise FileExistsError(
            f"{paths.ledger} exists: today's session already ran. Move it aside to run again."
        )
    assert config.sim.starting_collateral is not None
    source = create_market_data(config.venue)
    try:
        registry = InstrumentRegistry(source)
        await registry.refresh()
        instruments = [registry.by_symbol(s) for s in config.enabled_symbols]
        history, bars = await _load_market_history(source, instruments, calendar, day, now, config)
        return await _run(
            config, calendar, window, day, paths, source, instruments, history, bars,
            playbooks, secrets, stop,
        )  # fmt: skip
    finally:
        await source.aclose()


async def _load_market_history(
    source: MarketDataSource,
    instruments: Sequence[Instrument],
    calendar: TradingCalendar,
    day: date,
    now: datetime,
    config: AppConfig,
) -> tuple[dict[str, tuple[SessionSummary, ...]], dict[str, MinuteBars]]:
    window = calendar.trading_window(day)
    assert window is not None
    start = min(now, window.market.open) - LOOKBACK - timedelta(minutes=30)
    history: dict[str, tuple[SessionSummary, ...]] = {}
    bars: dict[str, MinuteBars] = {}
    for instrument in instruments:
        history[instrument.symbol] = await load_history(
            source, instrument.instrument_id, calendar.exchange, day, config.features
        )
        minute_bars = MinuteBars()
        minute_bars.seed(await source.fetch_candles(instrument.instrument_id, "1m", start, now))
        bars[instrument.symbol] = minute_bars
    return history, bars


async def _run(
    config: AppConfig,
    calendar: TradingCalendar,
    window: TradingWindow,
    day: date,
    paths: SessionPaths,
    source: MarketDataSource,
    instruments: Sequence[Instrument],
    history: Mapping[str, Sequence[SessionSummary]],
    bars: Mapping[str, MinuteBars],
    playbooks: Sequence[Playbook[Any]],
    secrets: Sequence[str],
    stop: asyncio.Event,
) -> int:
    fees = FeeRates(taker=config.sim.taker_fee_rate, maker=config.sim.maker_fee_rate)
    assert config.sim.starting_collateral is not None
    simulator = PaperSimulator(instruments, config.sim)
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger_sink = JsonlLedgerSink(paths.ledger)
    journal_sink = JsonlJournalSink(paths.journal, secrets=secrets)
    incident_sink = JsonlIncidentSink(paths.incidents)
    equity_sink = JsonlEquitySink(paths.equity)
    latency_sink = JsonlLatencySink(paths.agent_events)
    recorder = (
        Recorder(
            Path(config.market.recording_dir), {i.instrument_id: i.symbol for i in instruments}
        )
        if config.market.record
        else None
    )
    try:
        ledger = Ledger(config.sim.starting_collateral, sink=ledger_sink)
        risk = RiskEngine(config.risk, config.symbols, calendar, fees)
        execution = ExecutionEngine(
            SimBackend(simulator), ledger, risk, instruments, config.execution
        )
        plans = ExitPlanManager(
            execution,
            config.execution,
            cost_rate=2 * fees.taker + config.risk.stop_slippage_bp / 10_000,
        )
        journal = Journal(sink=journal_sink)
        journal.start_session(day, config.mode, config.enabled_symbols)
        engine_box: list[SessionEngine] = []
        feed = MarketFeed(
            source,
            instruments,
            config.market,
            recorder=recorder,
            on_event=lambda e: engine_box[0].queue_event(e),
            on_rest=lambda b, t, tr: engine_box[0].queue_rest(b, t, tr),
        )
        engine = SessionEngine(
            config=config,
            instruments=instruments,
            calendar=calendar,
            simulator=simulator,
            execution=execution,
            plans=plans,
            playbooks=playbooks,
            fees=fees,
            snapshot=feed.snapshot,
            history=history,
            bars=bars,
        )
        engine_box.append(engine)
        bridge = AgentBridge(
            config=config,
            market=engine,
            instruments=instruments,
            risk=risk,
            execution=execution,
            plans=plans,
            journal=journal,
            calendar=calendar,
            playbooks=playbooks,
            # Gate 1 (WP13) has not screened any playbook version yet; the
            # owner-exempt initial versions count as screened.
            screened=config.agent.exempt_playbooks,
            briefing=lambda: briefing_for(
                config, calendar, day, playbooks, config.agent.exempt_playbooks
            ),
            latency_sink=latency_sink,
        )
        engine.bridge = bridge
        guardian = Guardian(
            config=config.guardian,
            window=window,
            execution=execution,
            incidents=IncidentLog(sink=incident_sink),
            symbols=engine.symbols,
            snapshot=feed.snapshot,
            agent=bridge,
        )
        sampler = EquitySampler(
            execution, equity_sink, timedelta(seconds=config.guardian.equity_sample_seconds)
        )
        await execution.start_day(day)
        server = BridgeServer(bridge, paths.socket)
        await server.start()
        log.info(
            "paper session started", extra={"day": day.isoformat(), "socket": str(paths.socket)}
        )
        feed_stop = asyncio.Event()
        feed_task = asyncio.create_task(feed.run(feed_stop))
        try:
            await _loop(engine, guardian, sampler, stop)
        finally:
            feed_stop.set()
            await feed_task
            bridge.end_session()
            await server.close()
            bridge.flush_latency()
            journal.record_outcomes(ledger)
            await sampler.sample(utc_now(), force=True)
        return 0 if engine.flat else 1
    finally:
        for sink in (ledger_sink, journal_sink, incident_sink, equity_sink, latency_sink):
            sink.close()
        if recorder is not None:
            recorder.close()
        try:
            write_report(paths.day_dir)
        except Exception:
            log.exception("daily report failed", extra={"day": day.isoformat()})


async def _loop(
    engine: SessionEngine, guardian: Guardian, sampler: EquitySampler, stop: asyncio.Event
) -> None:
    """Pump, step, then the guardian, each failure recorded without stopping the others."""
    next_step = utc_now()
    while not stop.is_set():
        try:
            await engine.pump()
        except Exception as error:
            log.exception("market pump failed")
            guardian.record_error("market pump", error)
        now = utc_now()
        if now >= next_step:
            try:
                await engine.step(now)
            except Exception as error:
                log.exception("engine step failed")
                guardian.record_error("engine step", error, now)
            try:
                await guardian.check(now)
            except Exception:
                log.critical("guardian check failed", exc_info=True)
            try:
                await sampler.sample(now)
            except Exception:
                log.exception("equity sample failed")
            next_step = now + STEP_INTERVAL
            if guardian.session_over(now):
                break
        try:
            await asyncio.wait_for(stop.wait(), PUMP_INTERVAL.total_seconds())
        except TimeoutError:
            pass
