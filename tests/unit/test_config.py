from datetime import time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from perps_agent.config import (
    ConfigError,
    Mode,
    load_config,
    load_secrets,
    readiness_problems,
    require_ready,
)
from perps_agent.config.models import MarketDataConfig, SessionConfig, SimConfig
from tests.conftest import COMPLETE_RISK, COMPLETE_SIM, REPO_ROOT

LIVE_ENV_CREDS = {
    "PERPS_PROXY_ADDRESS": "0x0000000000000000000000000000000000000001",
    "PERPS_PROXY_SECRET": "proxy-secret-value",
    "PERPS_PROXY_PRIVATE_KEY": "0x" + "ab" * 32,
}


def test_repository_default_config_loads():
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    assert config.mode is Mode.PAPER
    assert config.enabled_symbols == (
        "NVDA-USD",
        "TSLA-USD",
        "MSFT-USD",
        "HOOD-USD",
        "MSTR-USD",
        "WTIOIL-USD",
    )
    assert config.session.window_start == time(9, 30)
    assert config.session.hard_flat_deadline == time(12, 35)
    assert config.live.enabled is False


def test_repository_default_config_is_ready_for_paper_without_credentials():
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    assert readiness_problems(config, load_secrets({}), {}) == []


def test_config_is_not_ready_until_risk_is_set(write_config):
    config = load_config(write_config({"sim": COMPLETE_SIM}))
    problems = readiness_problems(config, load_secrets({}), {})
    assert "risk.per_trade_risk_fraction is not set" in problems
    with pytest.raises(ConfigError):
        require_ready(config, load_secrets({}), {})


def test_complete_paper_config_is_ready_without_credentials(write_config):
    config = load_config(write_config({"risk": COMPLETE_RISK, "sim": COMPLETE_SIM}))
    assert readiness_problems(config, load_secrets({}), {}) == []


def test_paper_and_backtest_need_starting_collateral(write_config):
    for mode in (Mode.PAPER, Mode.BACKTEST):
        config = load_config(write_config({"risk": COMPLETE_RISK}), mode=mode)
        assert readiness_problems(config, load_secrets({}), {}) == [
            "sim.starting_collateral is not set"
        ]


def test_sim_defaults_are_tier_zero_fees():
    sim = SimConfig()
    assert (sim.taker_fee_rate, sim.maker_fee_rate) == (Decimal("0.0004"), Decimal("0.000125"))
    assert sim.starting_collateral is None


def test_yaml_floats_become_exact_decimals(write_config):
    config = load_config(write_config({"risk": COMPLETE_RISK}))
    assert config.risk.per_trade_risk_fraction == Decimal("0.005")
    assert config.risk.l2_risk_fraction == Decimal("0.5")


def test_mode_override(write_config):
    config = load_config(write_config({"mode": "paper"}), mode=Mode.BACKTEST)
    assert config.mode is Mode.BACKTEST


@pytest.mark.parametrize(
    "main",
    [
        {"unknown_key": 1},
        {"risk": {"per_trade_risk_fraction": 0}},
        {"risk": {"per_trade_risk_fraction": 1.5}},
        {"venue": {"api_url": "http://insecure.example"}},
        {"session": {"timezone": "Mars/Olympus"}},
        {"session": {"entry_cutoff": "12:45"}},
    ],
)
def test_invalid_config_is_rejected(write_config, main):
    with pytest.raises(ConfigError):
        load_config(write_config(main))


def test_unquoted_yaml_time_is_rejected_with_clear_message(tmp_path):
    (tmp_path / "symbols").mkdir()
    (tmp_path / "symbols" / "NVDA-USD.yaml").write_text("symbol: NVDA-USD\n")
    path = tmp_path / "default.yaml"
    path.write_text("symbols: [NVDA-USD]\nsession:\n  window_end: 12:30\n")
    with pytest.raises(ConfigError, match='quoted "HH:MM"'):
        load_config(path)


def test_session_times_must_be_ordered():
    with pytest.raises(ValidationError):
        SessionConfig(window_start=time(12, 0), entry_cutoff=time(11, 0))


@pytest.mark.parametrize(
    "main",
    [
        {"api_key": "x"},
        {"venue": {"proxy_secret": "x"}},
        {"live": {"private_key": "x"}},
    ],
)
def test_secret_like_keys_are_refused(write_config, main):
    with pytest.raises(ConfigError, match="secret-like keys"):
        load_config(write_config(main))


def test_symbol_file_must_match_its_name(write_config):
    with pytest.raises(ConfigError, match="'symbol' must be"):
        load_config(write_config({}, {"NVDA-USD": {"symbol": "TSLA-USD"}}))


def test_missing_symbol_file(write_config, tmp_path):
    path = write_config({})
    (tmp_path / "symbols" / "NVDA-USD.yaml").unlink()
    with pytest.raises(ConfigError, match="not found"):
        load_config(path)


def test_at_least_one_symbol_enabled(write_config):
    with pytest.raises(ConfigError):
        load_config(write_config({}, {"NVDA-USD": {"symbol": "NVDA-USD", "enabled": False}}))


def _live_config(write_config, **live):
    main = {"mode": "live", "risk": COMPLETE_RISK, "live": live}
    return load_config(write_config(main))


def test_live_mode_ready_only_when_enabled_attested_credentialed_and_not_dev(write_config):
    config = _live_config(write_config, enabled=True, jurisdiction_attestation="Permitted, XX")
    assert readiness_problems(config, load_secrets(LIVE_ENV_CREDS), LIVE_ENV_CREDS) == []


@pytest.mark.parametrize(
    ("live", "env", "expected"),
    [
        ({"enabled": False, "jurisdiction_attestation": "XX"}, {}, "live.enabled is false"),
        ({"enabled": True}, {}, "jurisdiction_attestation is missing"),
        ({"enabled": True, "jurisdiction_attestation": "XX"}, {"CI": "true"}, "dev/CI"),
        (
            {"enabled": True, "jurisdiction_attestation": "XX"},
            {"CLAUDE_CODE_REMOTE": "true"},
            "dev/CI",
        ),
    ],
)
def test_live_gate_refusals(write_config, live, env, expected):
    config = _live_config(write_config, **live)
    environ = {**LIVE_ENV_CREDS, **env}
    problems = readiness_problems(config, load_secrets(environ), environ)
    assert any(expected in p for p in problems)


def test_live_mode_requires_credentials(write_config):
    config = _live_config(write_config, enabled=True, jurisdiction_attestation="XX")
    problems = readiness_problems(config, load_secrets({}), {})
    assert any("PERPS_PROXY_SECRET" in p for p in problems)
    assert any("PERPS_PROXY_PRIVATE_KEY" in p for p in problems)


def test_live_enabled_is_refused_outside_live_mode(write_config):
    main = {"mode": "paper", "risk": COMPLETE_RISK, "live": {"enabled": True}}
    config = load_config(write_config(main))
    assert "live.enabled must be false in paper mode" in readiness_problems(
        config, load_secrets({}), {}
    )


def test_secrets_never_appear_in_repr_or_dump():
    secrets = load_secrets(LIVE_ENV_CREDS)
    text = repr(secrets) + str(secrets.model_dump()) + secrets.model_dump_json()
    assert "proxy-secret-value" not in text
    assert LIVE_ENV_CREDS["PERPS_PROXY_PRIVATE_KEY"] not in text
    assert set(secrets.secret_values()) == {
        "proxy-secret-value",
        LIVE_ENV_CREDS["PERPS_PROXY_PRIVATE_KEY"],
    }


def test_market_defaults_and_resubscribe_must_exceed_stale():
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    assert config.market.stale_after_ms == 2_000
    assert config.market.resubscribe_after_ms > config.market.stale_after_ms
    with pytest.raises(ValidationError):
        MarketDataConfig(stale_after_ms=5_000, resubscribe_after_ms=5_000)
