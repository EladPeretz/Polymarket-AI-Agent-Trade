from decimal import Decimal

from perps_agent.agent.context import flow_view, market_view
from perps_agent.venue import Side
from perps_agent.venue.models import Trade
from tests.unit.risk_factories import NOW, snapshot
from tests.unit.venue_factories import ticker


def _trade(trade_id: int, aggressor: Side, quantity: str) -> Trade:
    return Trade(
        trade_id=trade_id,
        instrument_id=19,
        aggressor=aggressor,
        price=Decimal(200),
        quantity=Decimal(quantity),
        timestamp=NOW,
    )


def test_flow_splits_recent_volume_by_aggressor():
    trades = [_trade(1, Side.BUY, "3"), _trade(2, Side.SELL, "1")]
    assert flow_view(trades) == {"trades": 2, "volume": "4", "buy_share": "0.75"}
    assert flow_view([]) == {"trades": 0, "volume": "0", "buy_share": None}


def test_market_view_shows_positioning_and_flow():
    perp = ticker("201").model_copy(update={"index_price": Decimal(200)})
    snap = snapshot().model_copy(
        update={"ticker": perp, "recent_trades": (_trade(1, Side.SELL, "2"),)}
    )
    view = market_view(snap)
    assert view["open_interest"] == "1000"
    assert view["premium_bp"] == "50.0"
    assert view["flow"] == {"trades": 1, "volume": "2", "buy_share": "0.00"}
