"""The owner's six playbooks (``decisions/owner-inputs.md``) and their registry.

Parameters come from defaults or from the agent's tuned values (WP14); either
way they are validated against the bounds defined here, in ``main``.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from perps_agent.strategy.playbooks.base import (
    ANY_REGIME,
    MAX_HOLD_MINUTES,
    Applicability,
    Conviction,
    ParameterBound,
    Playbook,
    PlaybookContext,
    PlaybookKind,
    PlaybookParams,
    Setup,
    parameter_bounds,
)
from perps_agent.strategy.playbooks.liquidity_sweep import LiquiditySweep, LiquiditySweepParams
from perps_agent.strategy.playbooks.mean_reversion import MeanReversion, MeanReversionParams
from perps_agent.strategy.playbooks.momentum_scalp import MomentumScalp, MomentumScalpParams
from perps_agent.strategy.playbooks.opening_flush import OpeningFlush, OpeningFlushParams
from perps_agent.strategy.playbooks.trend_following import TrendFollowing, TrendFollowingParams
from perps_agent.strategy.playbooks.vwap_reversion import VwapReversion, VwapReversionParams
from perps_agent.strategy.snapshot import FeatureSnapshot

PLAYBOOK_TYPES: tuple[type[Playbook[Any]], ...] = (
    VwapReversion,
    MomentumScalp,
    MeanReversion,
    LiquiditySweep,
    TrendFollowing,
    OpeningFlush,
)
PLAYBOOKS_BY_ID = {p.id: p for p in PLAYBOOK_TYPES}


class PlaybookParamsError(ValueError):
    """Parameters for an unknown playbook, or outside a playbook's bounds."""


def load_playbooks(
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[Playbook[Any], ...]:
    """Every playbook, with ``overrides`` (by playbook id) applied over the defaults."""
    overrides = overrides or {}
    unknown = set(overrides) - set(PLAYBOOKS_BY_ID)
    if unknown:
        raise PlaybookParamsError(f"unknown playbooks: {', '.join(sorted(unknown))}")
    playbooks = []
    for kind in PLAYBOOK_TYPES:
        try:
            params = kind.params_type.model_validate(dict(overrides.get(kind.id, {})))
        except ValidationError as error:
            raise PlaybookParamsError(f"{kind.id}: {error}") from error
        playbooks.append(kind(params))
    return tuple(playbooks)


def evaluate_all(
    playbooks: tuple[Playbook[Any], ...], features: FeatureSnapshot, context: PlaybookContext
) -> tuple[Setup, ...]:
    """Every setup the playbooks see now, in registry order."""
    setups = (p.evaluate(features, context) for p in playbooks)
    return tuple(s for s in setups if s is not None)


__all__ = [
    "ANY_REGIME",
    "MAX_HOLD_MINUTES",
    "PLAYBOOKS_BY_ID",
    "PLAYBOOK_TYPES",
    "Applicability",
    "Conviction",
    "LiquiditySweep",
    "LiquiditySweepParams",
    "MeanReversion",
    "MeanReversionParams",
    "MomentumScalp",
    "MomentumScalpParams",
    "OpeningFlush",
    "OpeningFlushParams",
    "ParameterBound",
    "Playbook",
    "PlaybookContext",
    "PlaybookKind",
    "PlaybookParams",
    "PlaybookParamsError",
    "Setup",
    "TrendFollowing",
    "TrendFollowingParams",
    "VwapReversion",
    "VwapReversionParams",
    "evaluate_all",
    "load_playbooks",
    "parameter_bounds",
]
