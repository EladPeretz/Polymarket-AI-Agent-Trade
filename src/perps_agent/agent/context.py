"""Compact, self-contained views for the agent (overview §3.5, "Context management").

Every event carries the market it concerns and a short session state, so the
agent does not need to remember the whole morning and context compaction loses
nothing important. Views are plain JSON-ready dicts; numbers are strings,
rounded for reading (prices to 6 significant digits, bp to 0.1).
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from perps_agent.market import MarketSnapshot
from perps_agent.strategy import FeatureSnapshot
from perps_agent.strategy.playbooks import Setup

Json = dict[str, Any]


def price(value: Decimal | None) -> str | None:
    """A price (or any amount) to 6 significant digits, e.g. ``"351.2"``."""
    if value is None:
        return None
    if value == 0:
        return "0"
    # 6 significant digits, never fewer than whole units, in plain notation.
    places = min(0, value.adjusted() - 5)
    text = format(value.quantize(Decimal(1).scaleb(places)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def bp(value: Decimal | None) -> str | None:
    return None if value is None else format(value, ".1f")


def ratio(value: Decimal | None) -> str | None:
    return None if value is None else format(value, ".2f")


def clock(at: datetime, zone: Any) -> str:
    """``HH:MM:SS`` in the exchange time zone."""
    return at.astimezone(zone).strftime("%H:%M:%S")


def market_view(snapshot: MarketSnapshot) -> Json:
    ticker = snapshot.ticker
    bid, ask = snapshot.best_bid, snapshot.best_ask
    view: Json = {
        "mark": price(snapshot.mark_price),
        "bid": price(bid.price) if bid else None,
        "ask": price(ask.price) if ask else None,
        "bid_size": price(bid.quantity) if bid else None,
        "ask_size": price(ask.quantity) if ask else None,
        "spread_bp": bp(snapshot.spread_bp),
        "funding_rate": format(ticker.funding_rate, ".3e") if ticker else None,
        "stale": snapshot.stale,
    }
    if snapshot.stale:
        view["stale_reasons"] = list(snapshot.stale_reasons)
    return view


def features_view(features: FeatureSnapshot) -> Json:
    vol = features.volatility
    view: Json = {
        "price": price(features.price),
        "volatility": {
            "range_15m_bp": bp(vol.range_15m_bp),
            "range_60m_bp": bp(vol.range_60m_bp),
            "minute_vol_bp": bp(vol.minute_vol_bp),
            "expected_move_to_close_bp": bp(vol.expected_move_to_close_bp),
            "minutes_to_close": vol.minutes_to_close,
        },
        "regime": {
            "volatility": features.regime.volatility,
            "structure": features.regime.structure,
            "range_ratio": ratio(features.regime.range_ratio),
        },
    }
    session = features.session
    if session is not None:
        view["session"] = {
            "minutes": session.minutes_elapsed,
            "open": price(session.open),
            "high": price(session.high),
            "low": price(session.low),
            "vwap": price(session.vwap),
            "vs_open_bp": bp(session.price_vs_open_bp),
            "vs_vwap_bp": bp(session.price_vs_vwap_bp),
            "range_bp": bp(session.range_bp),
            "efficiency": ratio(session.efficiency),
            "vwap_crosses": session.vwap_crosses,
            "opening_range": (
                [price(session.opening_range_low), price(session.opening_range_high)]
                if session.opening_range_low is not None
                else None
            ),
            "relative_volume": ratio(session.relative_volume),
            # VWAP and the profile are time-weighted: too few minutes traded.
            "volume_sparse": True if session.volume_sparse else None,
        }
    profile = features.profile
    if profile is not None:
        view["volume_profile"] = {
            "poc": price(profile.poc),
            "vs_poc_bp": bp((features.price - profile.poc) / profile.poc * 10_000),
            "value_area": [price(profile.value_area_low), price(profile.value_area_high)],
        }
    if features.levels:
        view["levels"] = {level.name: price(level.price) for level in features.levels}
    ticker = features.ticker
    if ticker is not None:
        view["ticker"] = {
            "character": ticker.character,
            "return_1d_bp": bp(ticker.return_1d_bp),
            "return_5d_bp": bp(ticker.return_5d_bp),
            "usual_range_bp": bp(ticker.average_range_bp),
            "up_sessions": f"{ticker.up_sessions}/{ticker.sessions}",
        }
    return view


def setup_view(setup: Setup, *, screened: bool) -> Json:
    return {
        "playbook": setup.playbook,
        "screened": screened,
        "side": setup.direction,
        "order_type": "market" if setup.entry_price is None else "limit",
        "entry": price(setup.entry_price or setup.reference_price),
        "stop": price(setup.stop_price),
        "target": price(setup.target_price),
        "reward_risk": ratio(setup.reward_risk),
        "conviction": setup.conviction,
        "max_size": ratio(setup.max_size),
        "time_stop": setup.time_stop.isoformat(),
        "reasons": list(setup.reasons),
        **({"variant": setup.variant} if setup.variant else {}),
    }


def symbol_view(
    symbol: str,
    snapshot: MarketSnapshot | None,
    features: FeatureSnapshot | None,
    setups: Iterable[Json],
    position: Json | None,
) -> Json:
    view: Json = {"symbol": symbol}
    if snapshot is not None:
        view["market"] = market_view(snapshot)
    if features is not None:
        view["features"] = features_view(features)
    view["setups"] = list(setups)
    if position is not None:
        view["position"] = position
    return view


def drop_none(data: Mapping[str, Any]) -> Json:
    return {k: v for k, v in data.items() if v is not None}
