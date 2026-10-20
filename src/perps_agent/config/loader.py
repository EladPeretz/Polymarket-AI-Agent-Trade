"""Load and validate configuration from YAML, and secrets from the environment."""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from perps_agent.config.models import AppConfig, Mode, Secrets

DEFAULT_CONFIG_PATH = Path("config/default.yaml")

# Keys that look like credentials are refused anywhere in config files.
_SECRET_KEY = re.compile(r"secret|private[_-]?key|password|passphrase|api[_-]?key", re.I)

# Environments where live trading must never start (ADR-0004).
_DEV_OR_CI_MARKERS = ("CI", "GITHUB_ACTIONS", "CLAUDE_CODE_REMOTE")

_SECRET_ENV = {
    "perps_proxy_address": "PERPS_PROXY_ADDRESS",
    "perps_proxy_secret": "PERPS_PROXY_SECRET",
    "perps_proxy_private_key": "PERPS_PROXY_PRIVATE_KEY",
}


class ConfigError(Exception):
    """Configuration is invalid or not ready for the requested mode."""


def load_config(path: Path = DEFAULT_CONFIG_PATH, *, mode: Mode | None = None) -> AppConfig:
    """Load the main config file and the per-symbol files it lists.

    The main file lists symbol names; each is read from ``symbols/<name>.yaml``
    next to the main file.
    """
    raw = _read_yaml(path)
    names = raw.pop("symbols", None)
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ConfigError(f"{path}: 'symbols' must be a list of symbol names")

    symbols = []
    for name in names:
        symbol_path = path.parent / "symbols" / f"{name}.yaml"
        symbol_raw = _read_yaml(symbol_path)
        if symbol_raw.get("symbol") != name:
            raise ConfigError(f"{symbol_path}: 'symbol' must be {name!r}")
        symbols.append(symbol_raw)

    raw["symbols"] = symbols
    if mode is not None:
        raw["mode"] = mode.value
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as error:
        raise ConfigError(f"{path}: invalid configuration\n{error}") from error


def load_secrets(environ: Mapping[str, str] | None = None) -> Secrets:
    env = os.environ if environ is None else environ
    values = {field: env[var] for field, var in _SECRET_ENV.items() if env.get(var)}
    return Secrets.model_validate(values)


def readiness_problems(
    config: AppConfig, secrets: Secrets, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Reasons the engine must not start in ``config.mode``. Empty means ready."""
    env = os.environ if environ is None else environ
    problems = [f"risk.{name} is not set" for name in config.risk.unset_fields()]

    if config.mode is Mode.LIVE:
        if not config.live.enabled:
            problems.append("live.enabled is false")
        if not (config.live.jurisdiction_attestation or "").strip():
            problems.append("live.jurisdiction_attestation is missing")
        markers = [m for m in _DEV_OR_CI_MARKERS if env.get(m)]
        if markers:
            problems.append(f"live mode refused in dev/CI environment ({', '.join(markers)})")
        if secrets.perps_proxy_address is None or secrets.perps_proxy_secret is None:
            problems.append("PERPS_PROXY_ADDRESS / PERPS_PROXY_SECRET are not set")
        if secrets.perps_proxy_private_key is None:
            problems.append("PERPS_PROXY_PRIVATE_KEY is not set")
    elif config.live.enabled:
        problems.append(f"live.enabled must be false in {config.mode.value} mode")
    return problems


def require_ready(
    config: AppConfig, secrets: Secrets, environ: Mapping[str, str] | None = None
) -> None:
    problems = readiness_problems(config, secrets, environ)
    if problems:
        raise ConfigError(
            f"not ready for {config.mode.value} mode:\n" + "\n".join(f"- {p}" for p in problems)
        )


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as error:
        raise ConfigError(f"config file not found: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: invalid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    secret_keys = list(_secret_like_keys(data))
    if secret_keys:
        raise ConfigError(
            f"{path}: secret-like keys are not allowed in config files "
            f"(use environment variables): {', '.join(secret_keys)}"
        )
    return data


def _secret_like_keys(data: Any, prefix: str = "") -> list[str]:
    found = []
    if isinstance(data, dict):
        for key, value in data.items():
            dotted = f"{prefix}{key}"
            if isinstance(key, str) and _SECRET_KEY.search(key):
                found.append(dotted)
            found.extend(_secret_like_keys(value, f"{dotted}."))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(_secret_like_keys(item, f"{prefix}{index}."))
    return found
