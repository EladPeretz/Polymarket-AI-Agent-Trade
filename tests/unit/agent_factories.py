"""A paper desk with the agent bridge on top, for bridge and exit-plan tests."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from perps_agent.agent import AgentBridge
from perps_agent.config.models import AppConfig, ExecutionConfig, SimConfig, SymbolConfig
from perps_agent.execution.plans import ExitPlanManager
from perps_agent.journal import Journal
from perps_agent.market import MarketSnapshot
from perps_agent.risk import Direction
from perps_agent.strategy import FeatureSnapshot
from perps_agent.strategy.playbooks import Conviction, Setup, load_playbooks
from tests.unit.execution_factories import Desk
from tests.unit.risk_factories import FEES, NOW, RISK, snapshot
from tests.unit.strategy_factories import CALENDAR, features, flat, minutes
from tests.unit.venue_factories import instrument

D = Decimal
SYMBOL = "NVDA-USD"
CONFIG = AppConfig(
    risk=RISK,
    sim=SimConfig(starting_collateral=D(500)),
    symbols=(SymbolConfig(symbol=SYMBOL),),
)
COST_RATE = 2 * FEES.taker + RISK.stop_slippage_bp / 10_000
PLAYBOOKS = load_playbooks()
VWAP_TAG = next(p.tag for p in PLAYBOOKS if p.id == "vwap_reversion")


def calm_features(price: str = "200") -> FeatureSnapshot:
    """An hour of flat bars to 10:00; the 20 bp floor binds the stop."""
    return features(flat(NOW - minutes(60), 60), price=price, at=NOW)


def setup(**overrides: Any) -> Setup:
    fields: dict[str, Any] = {
        "playbook": VWAP_TAG,
        "symbol": SYMBOL,
        "direction": Direction.LONG,
        "reference_price": D(200),
        "entry_price": D(200),
        "stop_price": D(199),
        "target_price": D(203),
        "time_stop": NOW + timedelta(hours=1),
        "conviction": Conviction.NORMAL,
        "max_size": D(1),
        "reward_risk": D("2.5"),
        "reasons": ("stretched from VWAP",),
    }
    fields.update(overrides)
    return Setup(**fields)


class FakeMarket:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.feature: FeatureSnapshot | None = calm_features()
        self.live_setups: tuple[Setup, ...] = ()

    def snapshot(self, symbol: str) -> MarketSnapshot:
        d = self.desk
        return snapshot(bid=d.bid, ask=d.ask, mark=d.mark, taken_at=d.clock.now)

    def features(self, symbol: str) -> FeatureSnapshot | None:
        return self.feature

    def setups(self, symbol: str) -> tuple[Setup, ...]:
        return self.live_setups


class AgentDesk(Desk):
    def __init__(
        self, *, screened: tuple[str, ...] = (), config: AppConfig = CONFIG, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.view = FakeMarket(self)
        self.plans = ExitPlanManager(
            self.execution, ExecutionConfig(), cost_rate=COST_RATE, clock=self.clock
        )
        self.journal = Journal(clock=self.clock)
        self.bridge = AgentBridge(
            config=config,
            market=self.view,
            instruments=[instrument()],
            risk=self.risk,
            execution=self.execution,
            plans=self.plans,
            journal=self.journal,
            calendar=CALENDAR,
            playbooks=PLAYBOOKS,
            screened=screened,
            briefing=lambda: "brief",
            clock=self.clock,
        )

    def call(self, tool: str, **args: Any) -> Any:
        return self.run(self.bridge.call(tool, args))

    def sync(self) -> None:
        self.run(self.bridge.sync())


def proposal(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "symbol": SYMBOL,
        "side": "long",
        "source": "discretionary",
        "order_type": "market",
        "size": "1",
        "stop_price": "199",
        "thesis": "holding VWAP after a flush",
        "confidence": "0.6",
        "scenarios": [
            {
                "name": "bounce",
                "description": "rotates back up",
                "probability": "0.6",
                "expected_r": "2",
                "assumptions": [{"key": "vwap_holds", "statement": "VWAP holds as support"}],
            },
            {"name": "flush", "description": "stops out", "probability": "0.4", "expected_r": "-1"},
        ],
        "chosen_scenario": "bounce",
        "expected_r": "0.8",
    }
    fields.update(overrides)
    return fields
