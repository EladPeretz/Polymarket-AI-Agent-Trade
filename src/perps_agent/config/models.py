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
    max_spread_bp: PositiveDecimal | None = None
    min_top_of_book_notional: PositiveDecimal | None = None
    # Stop must be at most this fraction of the distance from entry to liquidation.
    liquidation_buffer_fraction: Fraction | None = None
    max_snapshot_age_ms: Annotated[int, Field(ge=1)] = 2_000
    # Expected slippage of the market order a stop sends, beyond its trigger price.
    # Sizing counts it as a cost, with the fees.
    stop_slippage_bp: Annotated[Decimal, Field(ge=0, le=500)] = Decimal(5)

    def unset_fields(self) -> list[str]:
        return sorted(name for name, value in self if value is None)


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
    # Exits are reduce-only IOC orders limited this far through the mark, so a
    # thin book cannot fill them at any price.
    exit_slippage_bp: Annotated[Decimal, Field(gt=0, le=500)] = Decimal(50)


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
    risk: RiskConfig = RiskConfig()
    sim: SimConfig = SimConfig()
    execution: ExecutionConfig = ExecutionConfig()
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
