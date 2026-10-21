"""Typed configuration. Files hold no secrets; credentials come from the environment."""

import re
from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
Fraction = Annotated[Decimal, Field(gt=0, le=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class VenueConfig(_Model):
    api_url: str = "https://api.perpetuals.polymarket.com"
    ws_url: str = "wss://ws.perpetuals.polymarket.com/v1/ws"

    @field_validator("api_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("api_url must use https://")
        return value.rstrip("/")

    @field_validator("ws_url")
    @classmethod
    def _wss(cls, value: str) -> str:
        if not value.startswith("wss://"):
            raise ValueError("ws_url must use wss://")
        return value


class SessionConfig(_Model):
    """Trading window, in the exchange time zone."""

    timezone: str = "America/New_York"
    window_start: time = time(9, 30)
    entry_cutoff: time = time(12, 15)
    window_end: time = time(12, 30)
    hard_flat_deadline: time = time(12, 35)
    # Unscheduled NYSE closures (e.g. a national day of mourning) not yet in the
    # calendar module, as quoted "YYYY-MM-DD" strings.
    extra_closures: tuple[date, ...] = ()

    @field_validator(
        "window_start", "entry_cutoff", "window_end", "hard_flat_deadline", mode="before"
    )
    @classmethod
    def _hh_mm(cls, value: object) -> object:
        # Unquoted 12:30 in YAML 1.1 parses as the integer 750 (base-60), so only
        # "HH:MM" strings (or time objects) are accepted.
        if isinstance(value, time):
            if value.tzinfo is not None:
                raise ValueError("session times must not carry a time zone")
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
            raise ValueError(f'session times must be quoted "HH:MM" strings, got {value!r}')
        return value

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown time zone: {value}") from error
        return value

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not (
            self.window_start < self.entry_cutoff <= self.window_end <= self.hard_flat_deadline
        ):
            raise ValueError(
                "session times must satisfy "
                "window_start < entry_cutoff <= window_end <= hard_flat_deadline"
            )
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class AgentConfig(_Model):
    check_in_minutes: Annotated[int, Field(ge=1, le=60)] = 15
    # Kept below the cloud session's per-call time cap (10 minutes).
    next_event_timeout_seconds: Annotated[int, Field(ge=10, le=540)] = 540
    # Most events one ``next_event`` call returns; the rest wait for the next call.
    max_events_per_call: Annotated[int, Field(ge=1, le=50)] = 10
    # The engine serves the agent's tools on this Unix socket (ADR-0011).
    socket_path: str = "data/agent.sock"
    # Journals, ledgers and reports by date (ADR-0006); the briefing reads past days.
    results_dir: str = "results"
    # Playbooks that have not passed the Gate 1 screen still wake the agent and
    # appear in snapshots, labelled unscreened; the agent may act on them only as
    # L2 (discretionary) trades. Owner decision, 2026-10-21.
    unscreened_playbook_signals: bool = True
    # An entry rejected only for a thin book or a wide spread is evaluated again
    # on fresh snapshots, up to this many times this far apart, before the
    # rejection goes back to the agent. Owner decision, 2026-10-21.
    market_recheck_attempts: Annotated[int, Field(ge=0, le=20)] = 6
    market_recheck_interval_ms: Annotated[int, Field(ge=0, le=2_000)] = 500


class MarketDataConfig(_Model):
    """Market data feed and recorder (overview §3.2).

    Book and ticker frames arrive every 100 ms (measured 2026-10-20), so a quiet
    gap of ``stale_after_ms`` means the feed is broken, not that the market is
    calm. BBO and trades only arrive on change and are never required.
    """

    stale_after_ms: Annotated[int, Field(ge=200)] = 2_000
    # No frame at all for this long: close and reopen the subscription.
    resubscribe_after_ms: Annotated[int, Field(ge=1_000)] = 15_000
    check_interval_ms: Annotated[int, Field(ge=10, le=5_000)] = 250
    # Book levels per side in a snapshot. WebSocket book frames carry 20.
    snapshot_depth: Annotated[int, Field(ge=1, le=20)] = 10
    recent_trades_seconds: Annotated[int, Field(ge=1, le=3_600)] = 300
    record: bool = True
    # Raw recordings stay local (git-ignored); see ADR-0006.
    recording_dir: str = "data/recordings"

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.resubscribe_after_ms <= self.stale_after_ms:
            raise ValueError("resubscribe_after_ms must be greater than stale_after_ms")
        return self


class RiskConfig(_Model):
    """Risk rules (overview §3.7). ``None`` means not decided yet (capital-dependent).

    Every ``None`` must be set before the engine runs in any mode; see
    :meth:`unset_fields`.
    """

    per_trade_risk_fraction: Fraction | None = None
    daily_loss_limit_fraction: Fraction | None = None
    max_leverage: PositiveDecimal | None = None
    max_position_notional: PositiveDecimal | None = None
    max_concurrent_positions: Annotated[int, Field(ge=1)] = 1
    max_trades_per_day: Annotated[int, Field(ge=1)] | None = None
    max_l2_trades_per_day: Annotated[int, Field(ge=0)] | None = None
    l2_risk_fraction: Fraction = Decimal("0.5")
    min_stop_distance_bp: PositiveDecimal | None = None
    # Volatility stop floor: a stop is at least this many times the symbol's
    # recent 15-minute range from entry (and never closer than min_stop_distance_bp).
    stop_floor_range_multiple: Annotated[Decimal, Field(ge=1, le=5)] = Decimal(1)
    max_spread_bp: PositiveDecimal | None = None
    # Resting notional on each side of the book, counting the levels within
    # book_depth_band_bp of that side's best price (0: the best level only).
    min_book_depth_notional: PositiveDecimal | None = None
    book_depth_band_bp: Annotated[Decimal, Field(ge=0, le=200)] = Decimal(25)
    # Stop must be at most this fraction of the distance from entry to liquidation.
    liquidation_buffer_fraction: Fraction | None = None
    max_snapshot_age_ms: Annotated[int, Field(ge=1)] = 2_000
    # Expected slippage of the market order a stop sends, beyond its trigger price.
    # Sizing counts it as a cost, with the fees.
    stop_slippage_bp: Annotated[Decimal, Field(ge=0, le=500)] = Decimal(5)
    # A target, when set, must be worth at least this many times the risk to the
    # stop, costs included on both sides. Trades without a target are allowed.
    min_reward_risk: Annotated[Decimal, Field(ge=0, le=20)] = Decimal(2)

    def unset_fields(self) -> list[str]:
        return sorted(name for name, value in self if value is None)


class FeaturesConfig(_Model):
    """Feature and regime computation (overview §3.4). Not learnable (ADR-0010)."""

    opening_range_minutes: Annotated[int, Field(ge=1, le=60)] = 15
    # Width of a volume-profile price bucket, in bp of the session open.
    profile_bucket_bp: Annotated[Decimal, Field(gt=0, le=100)] = Decimal(5)
    value_area_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.7")
    # 1-minute returns used for realized volatility and the expected move.
    realized_vol_minutes: Annotated[int, Field(ge=10, le=240)] = 60
    # Prior sessions loaded for ticker performance, character and key levels.
    history_sessions: Annotated[int, Field(ge=2, le=40)] = 10
    # Regime: session range so far vs the usual range for the time elapsed.
    expansion_ratio: Annotated[Decimal, Field(gt=1, le=5)] = Decimal("1.3")
    compressed_ratio: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.7")
    # Regime: a trend moves at least this share of its range net, and crosses
    # VWAP at most this often.
    trend_efficiency: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.5")
    max_trend_vwap_crosses: Annotated[int, Field(ge=0, le=50)] = 3
    # No regime label before this much of the session has traded.
    regime_min_minutes: Annotated[int, Field(ge=1, le=60)] = 15


class TriggersConfig(_Model):
    """When the engine wakes the agent (overview §3.4). The check-in interval is in ``agent``."""

    # A large move: at least large_move_sigma × the 1-minute volatility scaled
    # to large_move_seconds, and at least large_move_min_bp.
    large_move_seconds: Annotated[int, Field(ge=5, le=900)] = 60
    large_move_sigma: Annotated[Decimal, Field(gt=0, le=20)] = Decimal(3)
    large_move_min_bp: Annotated[Decimal, Field(ge=0, le=500)] = Decimal(15)
    # Position events: the stop is this close (share of the entry-to-stop
    # distance left), the target this far along, or the time stop this near.
    approaching_stop_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.25")
    approaching_target_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.8")
    time_stop_warning_minutes: Annotated[int, Field(ge=0, le=30)] = 2


class SimConfig(_Model):
    """Paper/backtest fill simulator (overview §3.9, ADR-0005).

    Fee rates are the account's tier rates; with no account in paper mode they
    default to tier 0 (venue research §8). ``starting_collateral`` is capital,
    so it stays ``None`` until the owner sets it.
    """

    # An order reaches the simulated venue this long after it is sent, and
    # fills against the first book published at or after that moment.
    latency_ms: Annotated[int, Field(ge=0, le=5_000)] = 250
    taker_fee_rate: Annotated[Decimal, Field(ge=0, lt=Decimal("0.01"))] = Decimal("0.0004")
    # Negative at the top tiers (a rebate).
    maker_fee_rate: Annotated[Decimal, Field(gt=Decimal("-0.01"), lt=Decimal("0.01"))] = Decimal(
        "0.000125"
    )
    starting_collateral: PositiveDecimal | None = None


class ExecutionConfig(_Model):
    """Order handling (overview §3.8). Not capital-dependent, so it has defaults."""

    # A limit entry still working after this long is cancelled.
    entry_timeout_seconds: Annotated[int, Field(ge=1, le=3_600)] = 120
    # The longest timeout the agent may ask for on a single limit entry.
    max_entry_timeout_seconds: Annotated[int, Field(ge=1, le=3_600)] = 900
    # Exits are reduce-only IOC orders limited this far through the mark, so a
    # thin book cannot fill them at any price.
    exit_slippage_bp: Annotated[Decimal, Field(gt=0, le=500)] = Decimal(50)
    # Engine-run trailing stops and breakeven moves (ADR-0009): a stop moves at
    # most once per interval, and only by at least this step.
    trail_min_interval_seconds: Annotated[int, Field(ge=1, le=300)] = 5
    trail_min_step_bp: Annotated[Decimal, Field(ge=0, le=100)] = Decimal(2)


class GuardianConfig(_Model):
    """The guardian (overview §3.10): window close-out, kill switch, liveness, incidents.

    Not learnable (ADR-0010).
    """

    # Engaged for the whole session when true, or when ``kill_switch_file``
    # exists: everything is closed and no new entries are allowed.
    kill_switch: bool = False
    kill_switch_file: str = "data/KILL_SWITCH"
    # New entries are held while the agent has made no tool call for this long
    # (a call in progress, such as a waiting ``next_event``, counts as alive).
    agent_timeout_seconds: Annotated[int, Field(ge=60, le=3_600)] = 600
    # A symbol whose data stays stale this long inside the window is an incident.
    stale_incident_seconds: Annotated[int, Field(ge=1, le=3_600)] = 30
    # How often ledger and venue positions are compared; a difference seen on
    # two checks in a row holds new entries for the rest of the session.
    reconcile_interval_seconds: Annotated[int, Field(ge=5, le=3_600)] = 60
    # Equity is sampled this often for the daily report's drawdown.
    equity_sample_seconds: Annotated[int, Field(ge=5, le=3_600)] = 60


class ResultsConfig(_Model):
    """Publishing ``results/<date>/`` to the results branch (ADR-0006)."""

    branch: str = "paper-results"
    remote: str = "origin"
    # Push attempts, with 2, 4, 8 ... seconds between them.
    push_attempts: Annotated[int, Field(ge=1, le=8)] = 4

    @field_validator("branch")
    @classmethod
    def _not_main(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", value) or value in ("main", "master"):
            raise ValueError(f"results branch must be a dedicated branch name, got {value!r}")
        return value


class LiveConfig(_Model):
    """Live trading gate (ADR-0004). Off unless explicitly enabled and attested."""

    enabled: bool = False
    # Free-text statement naming the jurisdiction the live process runs from.
    jurisdiction_attestation: str | None = None


class AutonomyConfig(_Model):
    L0: bool = True
    L1: bool = True
    L2: bool = True


class SymbolConfig(_Model):
    symbol: str
    enabled: bool = True
    autonomy: AutonomyConfig = AutonomyConfig()
    # Short, factual notes on how the symbol trades, for the agent's briefing.
    notes: str | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol_format(cls, value: str) -> str:
        if not value or value != value.strip() or value.upper() != value:
            raise ValueError("symbol must be an upper-case venue symbol, e.g. NVDA-USD")
        return value


class AppConfig(_Model):
    mode: Mode = Mode.PAPER
    venue: VenueConfig = VenueConfig()
    session: SessionConfig = SessionConfig()
    agent: AgentConfig = AgentConfig()
    market: MarketDataConfig = MarketDataConfig()
    features: FeaturesConfig = FeaturesConfig()
    triggers: TriggersConfig = TriggersConfig()
    risk: RiskConfig = RiskConfig()
    sim: SimConfig = SimConfig()
    execution: ExecutionConfig = ExecutionConfig()
    guardian: GuardianConfig = GuardianConfig()
    results: ResultsConfig = ResultsConfig()
    live: LiveConfig = LiveConfig()
    symbols: tuple[SymbolConfig, ...]

    @model_validator(mode="after")
    def _unique_symbols(self) -> Self:
        names = [s.symbol for s in self.symbols]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate symbols: {names}")
        if not any(s.enabled for s in self.symbols):
            raise ValueError("at least one symbol must be enabled")
        return self

    @property
    def enabled_symbols(self) -> tuple[str, ...]:
        return tuple(s.symbol for s in self.symbols if s.enabled)


class Secrets(_Model):
    """Credentials, read from environment variables only. Paper mode needs none."""

    perps_proxy_address: str | None = None
    perps_proxy_secret: SecretStr | None = None
    perps_proxy_private_key: SecretStr | None = None

    def secret_values(self) -> list[str]:
        values = [self.perps_proxy_secret, self.perps_proxy_private_key]
        return [v.get_secret_value() for v in values if v is not None]
