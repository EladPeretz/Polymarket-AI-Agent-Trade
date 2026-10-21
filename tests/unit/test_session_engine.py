from datetime import timedelta
from decimal import Decimal

from perps_agent.app.session import SessionEngine, session_paths
from perps_agent.config.models import ExecutionConfig
from perps_agent.journal import TriggerKind
from perps_agent.risk import AutonomyLevel
from perps_agent.strategy import MinuteBars
from perps_agent.venue import Candle, Side, Trade, TradesEvent
from tests.unit.agent_factories import CONFIG, PLAYBOOKS, SYMBOL, AgentDesk
from tests.unit.risk_factories import FEES, NOW, intent, snapshot
from tests.unit.strategy_factories import CALENDAR, WINDOW, history, minutes

D = Decimal


def _seeded_bars() -> MinuteBars:
    bars = MinuteBars()
    bars.seed(
        Candle(
            instrument_id=19,
            interval="1m",
            open_time=NOW - minutes(90 - i),
            open=D(200),
            high=D("200.1"),
            low=D("199.9"),
            close=D(200),
            volume=D(10),
            trade_count=5,
        )
        for i in range(90)
    )
    return bars


def _engine(desk: AgentDesk) -> SessionEngine:
    engine = SessionEngine(
        config=CONFIG,
        instruments=[desk.execution._by_symbol[SYMBOL]],
        calendar=CALENDAR,
        simulator=desk.sim,
        execution=desk.execution,
        plans=desk.plans,
        playbooks=PLAYBOOKS,
        fees=FEES,
        snapshot=lambda symbol: snapshot(
            bid=desk.bid, ask=desk.ask, mark=desk.mark, taken_at=desk.clock.now
        ),
        history={SYMBOL: history()},
        bars={SYMBOL: _seeded_bars()},
    )
    engine.bridge = desk.bridge
    desk.view = engine  # the bridge reads features from the engine
    desk.bridge._market = engine
    return engine


def test_step_computes_features_and_wakes_the_agent():
    desk = AgentDesk()
    desk.market(0)
    engine = _engine(desk)
    desk.run(engine.step(NOW))
    features = engine.features(SYMBOL)
    assert features is not None and features.volatility.range_15m == D("0.2")
    answer = desk.call("next_event", timeout_seconds=0)
    keys = [e["key"] for e in answer["events"]]
    assert "session:open" in keys and "check_in" in keys


def test_trades_extend_the_bars():
    desk = AgentDesk()
    engine = _engine(desk)
    trade = Trade(
        trade_id=1,
        instrument_id=19,
        aggressor=Side.BUY,
        price=D(201),
        quantity=D(1),
        timestamp=NOW,
    )
    engine.queue_event(
        TradesEvent(channel="trades::19", sequence=1, received_at=NOW, trades=(trade,))
    )
    desk.run(engine.pump())
    desk.run(engine.step(NOW + timedelta(seconds=1)))
    features = engine.features(SYMBOL)
    assert features is not None and features.bars[-1].close == D(201)


def test_exit_plan_actions_reach_the_agent():
    desk = AgentDesk()
    engine = _engine(desk)
    desk.market(0)
    desk.run(engine.step(NOW))  # features, for the risk engine's stop floor
    desk.call(
        "propose_trade",
        **{
            "symbol": SYMBOL,
            "side": "long",
            "source": "discretionary",
            "order_type": "market",
            "size": "1",
            "stop_price": "199",
            "thesis": "t",
            "confidence": "0.5",
            "scenarios": [{"name": "up", "description": "up", "probability": "1"}],
            "chosen_scenario": "up",
            "exit_plan": {"breakeven_after_r": "1"},
        },
    )
    desk.market(300)
    desk.market(1_000, mark="201.5", bid="201.48", ask="201.52")
    desk.run(engine.step(desk.clock.now))
    events = desk.call("next_event", timeout_seconds=0)["events"]
    plan_events = [e for e in events if e["key"] == "position:exit_plan"]
    assert plan_events and "stop moved" in plan_events[0]["detail"]


def test_the_engine_leaves_the_window_close_to_the_guardian():
    desk = AgentDesk()
    engine = _engine(desk)
    desk.market(0)
    desk.enter(intent(entry=None, stop="199", level=AutonomyLevel.L2))
    desk.market(300)
    end = WINDOW.end
    desk.clock.now = end
    desk.run(engine.step(end))
    desk.market((end - NOW) // timedelta(milliseconds=1) + 300)
    assert not engine.flat


def test_session_paths(tmp_path):
    config = CONFIG.model_copy(
        update={"agent": CONFIG.agent.model_copy(update={"results_dir": str(tmp_path)})}
    )
    paths = session_paths(config, NOW.date())
    assert paths.journal == tmp_path / "2026-10-20" / "journal.jsonl"
    assert paths.ledger == tmp_path / "2026-10-20" / "ledger.jsonl"
    assert paths.incidents == tmp_path / "2026-10-20" / "incidents.jsonl"
    assert paths.equity == tmp_path / "2026-10-20" / "equity.jsonl"
    assert paths.agent_events == tmp_path / "2026-10-20" / "agent_events.jsonl"


def test_execution_config_defaults_for_trailing():
    config = ExecutionConfig()
    assert config.trail_min_interval_seconds == 5 and config.trail_min_step_bp == D(2)
    assert TriggerKind.POSITION == "position"


def test_equity_sampler_samples_on_its_interval():
    from perps_agent.app.session import EquitySampler

    desk = AgentDesk()
    samples = []
    sampler = EquitySampler(desk.execution, samples.append, timedelta(seconds=60))
    desk.run(sampler.sample(NOW))
    desk.run(sampler.sample(NOW + timedelta(seconds=30)))
    desk.run(sampler.sample(NOW + timedelta(seconds=60)))
    desk.run(sampler.sample(NOW + timedelta(seconds=61), force=True))
    assert len(samples) == 3
    assert samples[0].equity == D(500) and samples[0].open_positions == 0


def test_the_loop_survives_a_failing_step_and_still_runs_the_guardian():
    import asyncio

    from perps_agent.app.session import _loop

    class Broken:
        async def pump(self) -> None:
            pass

        async def step(self, now) -> None:
            raise RuntimeError("feature bug")

    class Watch:
        def __init__(self) -> None:
            self.errors: list[str] = []
            self.checks = 0

        def record_error(self, where, error, now=None) -> None:
            self.errors.append(f"{where}: {error}")

        async def check(self, now) -> None:
            self.checks += 1

        def session_over(self, now) -> bool:
            return self.checks >= 1

    class NoSamples:
        async def sample(self, now, *, force=False) -> None:
            pass

    watch = Watch()
    asyncio.run(_loop(Broken(), watch, NoSamples(), asyncio.Event()))  # type: ignore[arg-type]
    assert watch.errors == ["engine step: feature bug"] and watch.checks == 1
