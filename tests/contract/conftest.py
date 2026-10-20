"""Serve recorded venue responses to the real adapter through an httpx mock transport."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from perps_agent.venue.polymarket_source import PolymarketMarketData
from perps_agent.venue.retry import RetryPolicy

FIXTURES = Path(__file__).parent / "fixtures"
API_URL = "https://api.perpetuals.polymarket.com"
WS_URL = "wss://ws.perpetuals.polymarket.com/v1/ws"

ROUTES = {
    "/v1/info/instruments": "instruments.json",
    "/v1/info/tickers": "tickers.json",
    "/v1/info/statistics": "statistics.json",
    "/v1/info/exchange": "exchange.json",
    "/v1/info/book": "book.json",
    "/v1/info/trades": "trades.json",
    "/v1/info/klines": "klines.json",
    "/v1/info/mark-history": "mark_history.json",
    "/v1/info/funding": "funding.json",
}


def load_rest(name: str) -> Any:
    return json.loads((FIXTURES / "rest" / name).read_text())


def load_frames() -> list[Any]:
    lines = (FIXTURES / "ws" / "frames.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class RecordedVenue:
    """Mock HTTP venue. Serves fixtures by path; ``overrides`` replace a route's handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.overrides:
            return self.overrides[path](request)
        if path not in ROUTES:
            return httpx.Response(404, json={"status": "err", "error": "not_found"})
        return httpx.Response(200, json=load_rest(ROUTES[path]))

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


@pytest.fixture
def venue() -> RecordedVenue:
    return RecordedVenue()


@pytest.fixture
def make_source(venue: RecordedVenue) -> Callable[..., PolymarketMarketData]:
    def _make(**kwargs: Any) -> PolymarketMarketData:
        kwargs.setdefault("retry", RetryPolicy(attempts=3, base_delay_seconds=0))
        return PolymarketMarketData(
            api_url=API_URL, ws_url=WS_URL, http_transport=httpx.MockTransport(venue), **kwargs
        )

    return _make
