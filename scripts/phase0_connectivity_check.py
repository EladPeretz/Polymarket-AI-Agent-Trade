"""Phase 0 read-only connectivity check for Polymarket Perps.

Never places, modifies or cancels orders, and never creates credentials.

Checks:
  1. Public REST: server time / clock skew, exchange status, instruments, book.
  2. Known SDK issue: single-ticker lookup returning the wrong instrument.
  3. Public WebSocket: BBO stream for one instrument.
  4. Optional: registration of POLYMARKET_EOA_ADDRESS (public lookup).
  5. Optional: private portfolio read if PERPS_PROXY_ADDRESS and
     PERPS_PROXY_SECRET are set (existing proxy credentials only).

Usage:
    python scripts/phase0_connectivity_check.py [--symbol NVDA-USD] [--ws-seconds 5]
"""

import argparse
import asyncio
import os
import sys
import time

import httpx
from polymarket import AsyncPublicClient
from polymarket.streams import PerpsBboSpec

API_URL = "https://api.perpetuals.polymarket.com"
MAX_CLOCK_SKEW_MS = 2_000


def report(ok: bool, name: str, detail: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


async def check_public(
    client: AsyncPublicClient, http: httpx.AsyncClient, symbol: str, ws_seconds: int
) -> list[bool]:
    results = []

    t0 = int(time.time() * 1000)
    server_ms = (await http.get("/v1/info/time")).json()["time"]
    skew = server_ms - (t0 + int(time.time() * 1000)) // 2
    results.append(report(abs(skew) < MAX_CLOCK_SKEW_MS, "clock skew", f"{skew} ms"))

    exchange = (await http.get("/v1/info/exchange")).json()
    results.append(
        report(
            not exchange["cancel_only"],
            "exchange status",
            f"engine {exchange['engine_version']}, cancel_only={exchange['cancel_only']}",
        )
    )

    instruments = await client.fetch_perps_instruments()
    equities = [i for i in instruments if i.category == "equity"]
    target = next((i for i in instruments if i.symbol == symbol), None)
    results.append(
        report(
            target is not None,
            "instruments",
            f"{len(instruments)} total, {len(equities)} equity, {symbol} "
            + (f"id={target.id} max_leverage={target.max_leverage}" if target else "NOT FOUND"),
        )
    )
    if target is None:
        return results

    book = await client.fetch_perps_book(instrument_id=target.id, depth=10)
    has_book = bool(book.bids and book.asks)
    detail = f"bid {book.bids[0].price} / ask {book.asks[0].price}" if has_book else "empty side"
    results.append(report(has_book, "order book", detail))

    # The tickers endpoint ignores instrument_id and the SDK returns the first
    # row, so filter by id ourselves. Report the SDK behaviour for visibility.
    sdk_ticker = await client.fetch_perps_ticker(instrument_id=target.id)
    tickers = await client.fetch_perps_tickers()
    ticker = next(t for t in tickers if t.instrument_id == target.id)
    print(
        f"[INFO] ticker lookup: SDK fetch_perps_ticker returned instrument "
        f"{sdk_ticker.instrument_id} (wanted {target.id}); filtered mark={ticker.mark_price} "
        f"index={ticker.index_price} funding/h={ticker.funding_rate}"
    )

    stream = await client.subscribe(PerpsBboSpec(instrument_id=target.id))
    events = 0
    deadline = time.monotonic() + ws_seconds
    try:
        async with asyncio.timeout(ws_seconds + 10):
            async for _ in stream:
                events += 1
                if time.monotonic() > deadline:
                    break
    except TimeoutError:
        pass
    finally:
        await stream.close()
    results.append(report(events > 0, "websocket bbo", f"{events} events in ~{ws_seconds}s"))
    return results


async def check_account(client: AsyncPublicClient, http: httpx.AsyncClient) -> list[bool]:
    results = []

    eoa = os.environ.get("POLYMARKET_EOA_ADDRESS")
    if eoa:
        registered = await client.fetch_perps_registration(address=eoa)
        results.append(report(registered, "perps registration", f"{eoa} registered={registered}"))
    else:
        print("[SKIP] perps registration: POLYMARKET_EOA_ADDRESS not set")

    proxy = os.environ.get("PERPS_PROXY_ADDRESS")
    secret = os.environ.get("PERPS_PROXY_SECRET")
    if proxy and secret:
        response = await http.get(
            "/v1/account/portfolio",
            headers={"polymarket-proxy": proxy, "polymarket-secret": secret},
        )
        if response.status_code == 200:
            portfolio = response.json()
            results.append(
                report(
                    True,
                    "private portfolio read",
                    f"account value {portfolio['margin']['total_account_value']}, "
                    f"positions {len(portfolio['positions'])}, "
                    f"fee tier {portfolio.get('fee_tier')}",
                )
            )
        else:
            results.append(report(False, "private portfolio read", f"HTTP {response.status_code}"))
    else:
        print("[SKIP] private portfolio read: PERPS_PROXY_ADDRESS / PERPS_PROXY_SECRET not set")
    return results


async def main(symbol: str, ws_seconds: int) -> int:
    async with (
        AsyncPublicClient() as client,
        httpx.AsyncClient(base_url=API_URL, timeout=20) as http,
    ):
        results = await check_public(client, http, symbol, ws_seconds)
        results += await check_account(client, http)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default="NVDA-USD")
    parser.add_argument("--ws-seconds", type=int, default=5)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.symbol, args.ws_seconds)))
