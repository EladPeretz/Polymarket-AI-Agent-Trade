"""Swing on an opening flush: buy a beaten-down ticker's reclaim after it flushes off the open.

Owner: beaten-down tickers that flush off the open. **Long only** for now.

Trigger: the ticker is beaten down (5-session return at most
``-beaten_down_bp``); within ``flush_window_minutes`` of the open it fell at
least ``min_flush_bp`` below the session open; price has reclaimed at least
``reclaim_fraction`` of the flush, and the last completed bar closed up and
above the previous bar's high. Valid until ``entry_deadline_minutes`` after
the open. Entry: limit at the current price. Stop: below the flush low, at
least the volatility floor. Target: ``target_r`` × the risk.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import Field

from perps_agent.risk import Direction
from perps_agent.strategy.features import BP
from perps_agent.strategy.playbooks.base import (
    ANY_REGIME,
    Playbook,
    PlaybookContext,
    PlaybookKind,
    PlaybookParams,
    Setup,
)
from perps_agent.strategy.snapshot import FeatureSnapshot


class OpeningFlushParams(PlaybookParams):
    beaten_down_bp: Annotated[
        Decimal, Field(ge=200, le=2_000, description="5-session loss at least this")
    ] = Decimal(500)
    flush_window_minutes: Annotated[int, Field(ge=5, le=60)] = 30
    min_flush_bp: Annotated[Decimal, Field(ge=30, le=400, description="below the open")] = Decimal(
        100
    )
    reclaim_fraction: Annotated[
        Decimal, Field(ge=Decimal("0.1"), le=Decimal("0.8"), description="of the flush")
    ] = Decimal("0.3")
    entry_deadline_minutes: Annotated[int, Field(ge=15, le=150)] = 90
    target_r: Annotated[Decimal, Field(ge=Decimal("2.1"), le=6, description="R, net of costs")] = (
        Decimal(3)
    )
    max_hold_minutes: Annotated[int, Field(ge=1, le=120)] = 120


class OpeningFlush(Playbook[OpeningFlushParams]):
    id = "opening_flush"
    logic_version = 1
    kind = PlaybookKind.INTRADAY
    regimes = (ANY_REGIME,)
    long_only = True
    params_type = OpeningFlushParams
    description = "Long a beaten-down ticker's reclaim after it flushes off the open."

    def _evaluate(self, features: FeatureSnapshot, context: PlaybookContext) -> Setup | None:
        p = self.params
        session, ticker = features.session, features.ticker
        assert session is not None
        if ticker is None or ticker.return_5d_bp is None:
            return None
        if ticker.return_5d_bp > -p.beaten_down_bp:
            return None
        if session.minutes_elapsed > p.entry_deadline_minutes:
            return None
        flush_end = session.started_at + timedelta(minutes=p.flush_window_minutes)
        bars = [b for b in features.completed_bars if b.start >= session.started_at]
        flush = [b for b in bars if b.start < flush_end]
        if len(bars) < 2 or not flush:
            return None
        low = min(b.low for b in flush)
        # The flush low must hold: nothing after the window traded below it.
        if any(b.low < low for b in bars if b.start >= flush_end):
            return None
        drop = session.open - low
        if drop <= 0 or drop / session.open * BP < p.min_flush_bp:
            return None
        if features.price < low + p.reclaim_fraction * drop:
            return None
        last, previous = bars[-1], bars[-2]
        if not (last.close > last.open and last.close > previous.high):
            return None
        return self._setup(
            features,
            context,
            Direction.LONG,
            structural_stop=low,
            target_r=p.target_r,
            hold_minutes=p.max_hold_minutes,
            reasons=(
                f"5-session return {ticker.return_5d_bp:+.0f} bp",
                f"flushed {drop / session.open * BP:.0f} bp below the open to {low}, reclaiming",
            ),
        )
