"""Venue adapter (ADR-0001): the only package that talks to Polymarket Perps.

Only this package may import the ``polymarket`` SDK, and within it only
``polymarket_source``. Everything else uses the interfaces and domain models
exported here.
"""

from perps_agent.config.models import VenueConfig
from perps_agent.venue.errors import (
    RateLimited,
    RequestRejected,
    SchemaMismatch,
    UnknownInstrument,
    VenueError,
    VenueUnavailable,
)
from perps_agent.venue.interfaces import (
    AccountReader,
    MarketDataSource,
    MarketStream,
    OrderGateway,
)
from perps_agent.venue.models import (
    Bbo,
    BboEvent,
    BookEvent,
    BookLevel,
    Candle,
    ExchangeStatus,
    Fill,
    FundingRate,
    Instrument,
    MarketEvent,
    MarkPoint,
    OrderBook,
    OrderRequest,
    OrderState,
    OrderStatus,
    Side,
    StreamChannel,
    Ticker,
    TickerEvent,
    TimeInForce,
    Trade,
    TradesEvent,
)
from perps_agent.venue.registry import InstrumentRegistry


def create_market_data(config: VenueConfig) -> MarketDataSource:
    """The real, read-only market data source for ``config``. Needs no credentials."""
    # Imported here so that loading the domain models does not load the SDK.
    from perps_agent.venue.polymarket_source import PolymarketMarketData

    return PolymarketMarketData(api_url=config.api_url, ws_url=config.ws_url)


__all__ = [
    "AccountReader",
    "Bbo",
    "BboEvent",
    "BookEvent",
    "BookLevel",
    "Candle",
    "ExchangeStatus",
    "Fill",
    "FundingRate",
    "Instrument",
    "InstrumentRegistry",
    "MarkPoint",
    "MarketDataSource",
    "MarketEvent",
    "MarketStream",
    "OrderBook",
    "OrderGateway",
    "OrderRequest",
    "OrderState",
    "OrderStatus",
    "RateLimited",
    "RequestRejected",
    "SchemaMismatch",
    "Side",
    "StreamChannel",
    "Ticker",
    "TickerEvent",
    "TimeInForce",
    "Trade",
    "TradesEvent",
    "UnknownInstrument",
    "VenueError",
    "VenueUnavailable",
    "create_market_data",
]
