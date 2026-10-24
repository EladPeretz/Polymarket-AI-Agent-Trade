"""Record public Polymarket Perps responses as contract-test fixtures.

Read-only: it calls public market-data endpoints only. It never authenticates,
never signs and never sends an order.

REST responses are stored exactly as the server sent them, trimmed to the
instruments named on the command line so the fixtures stay small. With
``--ws-seconds`` it also records raw WebSocket frames (the environment must
allow WebSocket connections; in a cloud session that needs Full network access).

Usage:
    python scripts/record_venue_fixtures.py [--symbols NVDA-USD] [--ws-seconds 10]

Re-run after an SDK upgrade or a venue API change, then run the contract tests.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.perpetuals.polymarket.com"
WS_URL = "wss://ws.perpetuals.polymarket.com/v1/ws"
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "contract" / "fixtures"
# SP500-USD (instrument 1) is the first row of /v1/info/tickers. It is kept so the
# ticker regression test reproduces the SDK returning the wrong instrument.
ALWAYS_KEEP = ("SP500-USD",)
WINDOW_MS = 15 * 60_000


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n")
    print(f"wrote {path.relative_to(FIXTURES.parents[2])}")


async def record_rest(http: httpx.AsyncClient, symbols: list[str]) -> dict[str, Any]:
    async def get(path: str, **params: Any) -> Any:
        response = await http.get(path, params=params or None)
        response.raise_for_status()
        return response.json()

    instruments = await get("/v1/info/instruments")
    keep = [i for i in instruments if i["symbol"] in {*symbols, *ALWAYS_KEEP}]
    missing = set(symbols) - {i["symbol"] for i in keep}
    if missing:
        raise SystemExit(f"unknown symbols: {sorted(missing)}")
    ids = {i["instrument_id"] for i in keep}
    rest = FIXTURES / "rest"
    _dump(rest / "instruments.json", keep)

    tickers = await get("/v1/info/tickers")
    _dump(rest / "tickers.json", [t for t in tickers if t["instrument_id"] in ids])
    statistics = await get("/v1/info/statistics")
    _dump(rest / "statistics.json", [s for s in statistics if s["instrument_id"] in ids])
    exchange = await get("/v1/info/exchange")
    _dump(rest / "exchange.json", exchange)

    now = int(time.time() * 1000)
    window = {"start_timestamp": now - WINDOW_MS, "end_timestamp": now}
    target = next(i for i in keep if i["symbol"] == symbols[0])
    iid = target["instrument_id"]
    _dump(rest / "book.json", await get("/v1/info/book", instrument_id=iid, depth=10))
    _dump(rest / "trades.json", await get("/v1/info/trades", instrument_id=iid, **window))
    _dump(
        rest / "klines.json",
        await get("/v1/info/klines", instrument_id=iid, interval="1m", **window),
    )
    _dump(
        rest / "mark_history.json",
        await get("/v1/info/mark-history", instrument_id=iid, interval="1m", **window),
    )
    day = {"start_timestamp": now - 24 * 3_600_000, "end_timestamp": now}
    _dump(rest / "funding.json", await get("/v1/info/funding", instrument_id=iid, **day))
    return {"instrument_id": iid, "engine_version": exchange.get("engine_version")}


async def record_ws(instrument_id: int, seconds: int) -> None:
    import websockets  # installed with the pinned SDK

    channels = [f"{c}::{instrument_id}" for c in ("bbo", "book", "trades", "tickers")]
    frames: list[Any] = []
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"id": 1, "req": "sub", "chs": channels}))
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except TimeoutError:
                break
            frames.append(json.loads(raw))
    # Keep the first few frames per channel (plus acks) so the fixture stays small.
    kept: list[Any] = []
    per_channel: dict[str | None, int] = {}
    for frame in frames:
        channel = frame.get("ch") if isinstance(frame, dict) else None
        per_channel[channel] = per_channel.get(channel, 0) + 1
        if per_channel[channel] <= 3:
            kept.append(frame)
    path = FIXTURES / "ws" / "frames.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(f) + "\n" for f in kept))
    print(f"wrote {len(kept)} of {len(frames)} frames to {path.relative_to(FIXTURES.parents[2])}")


async def main(symbols: list[str], ws_seconds: int) -> None:
    async with httpx.AsyncClient(base_url=API_URL, timeout=20) as http:
        meta = await record_rest(http, symbols)
    if ws_seconds > 0:
        await record_ws(meta["instrument_id"], ws_seconds)
    from importlib.metadata import version

    meta.update(
        recorded_at_ms=int(time.time() * 1000),
        sdk_version=version("polymarket-client"),
        symbols=symbols,
    )
    _dump(FIXTURES / "rest" / "manifest.json", meta)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", nargs="+", default=["NVDA-USD"])
    parser.add_argument("--ws-seconds", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(main(args.symbols, args.ws_seconds))
