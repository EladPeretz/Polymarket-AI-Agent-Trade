# Phase 0 — Venue Research: Polymarket Perps (stock perpetuals)

Status: complete · Researched: 2026-10-19 · Exchange engine version at time of research: `0.0.80`

This note captures everything we learned about Polymarket Perps before designing
the trading system. It is the input to Phase 1 (architecture). Facts come from the
official docs (all 27 Perps pages, the OpenAPI and AsyncAPI specs, the changelog)
and from live read-only probes of the public API. Where the docs and the live API
disagree, the live API is noted and wins.

Sources: <https://docs.polymarket.com/perps/overview>,
<https://docs.polymarket.com/api-reference/perps/overview>,
`/api-spec/perps-openapi.json`, `/asyncapi-perps.json`,
<https://docs.polymarket.com/changelog/perps>.

---

## 1. Summary

| Question | Answer |
| - | - |
| Is there a usable trading API? | Yes. Full REST + WebSocket API for market data, trading and account state, plus an official Python SDK (`polymarket-client`). |
| Are stock perps listed? | Yes. 38 `equity` instruments (plus 6 `index`, e.g. SP500/NAS100). |
| Is there historical data for backtests? | Only since listing: ~3 months (NVDA from 2026-07-20). Longer backtests need external stock data. |
| Does the existing Polymarket account carry over? | Partly. Same wallet/private key, but Perps uses its own proxy-key credentials, not the CLOB API key. See §3. |
| Blockers | **Geographic restriction (§4)**. Order placement is not permitted from the US, Canada and others. |

Verdict: the venue is technically viable for a stock-perps bot. Proceed to Phase 1
once the geographic question in §4 and the open questions in
[open-questions.md](open-questions.md) are answered.

---

## 2. Venue model

* **Hybrid exchange.** Matching, margin, funding and liquidation run offchain;
  deposits and withdrawals settle on Polygon (chain id 137). Trading itself
  produces no per-trade onchain transactions. State roots are committed onchain
  periodically.
* **Collateral:** pUSD only (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`).
  Perps deposit contract: `0xDCa4af75705dbB50f62437045afF9921947917d2`.
  Minimum deposit 10 pUSD. Raw amounts use 6 decimals (10 pUSD = `10_000_000`).
* **24/7 trading.** The book, matching, funding, margin checks and liquidation run
  continuously, including when the underlying stock market is closed. Only the
  price-feed set changes by session (§6).
* **Endpoints:** REST `https://api.perpetuals.polymarket.com`, WebSocket
  `wss://ws.perpetuals.polymarket.com/v1/ws`.

---

## 3. Access and authentication

Perps does **not** use the prediction-market (CLOB) API key / secret / passphrase.
It uses a delegated "proxy" (trading key):

1. **Owner key (EOA).** The wallet's private key signs a one-time EIP-712
   `CreateProxy` message (domain `{name: "Polymarket", version: "1", chainId: 137}`)
   authorizing a freshly generated proxy address, with an expiry (SDK default:
   1 week).
2. `POST /v1/account/proxy` returns a **proxy secret**. Store
   `{proxy_address, proxy_private_key, proxy_secret, expiry}` securely.
3. **Private reads:** headers `polymarket-proxy: <proxy_address>` and
   `polymarket-secret: <proxy_secret>`. The WebSocket uses an `auth` frame with the same pair.
4. **Trading commands:** each command is turned into a compact positional tuple
   (e.g. `["createOrders", [[iid, buy, p, qty, tif, po, ro, c, tr]]]`), `None`
   entries are dropped, the result is MessagePack-encoded and `keccak256`-hashed,
   and the hash is signed as an EIP-712 `Op{data, salt, ts}` **with the proxy
   key**. `ts` is in milliseconds and signatures older than 5 minutes are rejected.
   Fresh `salt` per request. Sync clocks against `GET /v1/info/time`.
5. **Withdrawals always require the owner key**, never the proxy. A
   compromised trading key can trade but cannot withdraw funds.

What this means for the existing account:

* The Perps account is keyed by the **EOA** that signs `CreateProxy`, not by a
  Safe/proxy wallet address. If the prediction-market account uses a Safe or
  Polymarket proxy wallet, Perps balances are still tracked against the
  underlying EOA.
* Registration is checked with `GET /v1/info/registered?address=<EOA>`. The
  `createProxy` body accepts an optional invite/referral `code`. Whether an
  unregistered address needs one is **not documented**; to be verified with the
  user's address (see open questions).
* **Funding the account** (`setup_trading_approvals` + `deposit_to_perps`) goes
  through Polymarket's relayer and needs a **Relayer API key**, created at
  polymarket.com → Settings → API Keys. Trading and reads do not need it. A
  manual deposit via the web app also works.

---

## 4. Geographic restrictions — BLOCKER

Order placement is not permitted from: **United States, Canada, Cuba, Iran,
North Korea, Syria, Crimea, Donetsk, Luhansk** (list can change). Read-only
market data is not restricted. Builders must block order submission, not just
warn.

Implications:

* The bot may only place orders when the operator is in a permitted
  jurisdiction **and** the trading process runs from infrastructure there.
* This cloud development container should be assumed to egress from a
  restricted region. It is fine for development, backtests and read-only data,
  but **live order placement must run elsewhere**.
* The system must have a hard, config-driven "live trading allowed" gate, off by
  default.

---

## 5. Stock perp instruments (live, 2026-10-19)

89 instruments: 41 crypto, 38 equity, 6 index, 4 commodity. Always read live
values from `GET /v1/info/instruments`, because parameters change.

Common parameters across all 38 equity perps:

| Parameter | Value |
| - | - |
| Max leverage | 10x (risk tiers reduce it: 5x above $250k–500k notional, 3x above $1M, 2x above $3M, 1x above $5M) |
| Maintenance margin rate | `0.5 / max_leverage` = **5%** of notional |
| Liquidation fee | 0.5% of notional per liquidation fill (on top of taker fee) |
| Min order notional | $10 |
| Max market-order notional | $500,000; max limit-order notional $2,500,000 |
| Price band | ±10% around mark (`price_bounds 0.10`) |
| Max orders per instrument | 200 |
| Funding interval | 1h |
| Margin modes | cross or isolated (none are isolated-only); default with no explicit setting is **isolated at max leverage**, so the bot must set leverage explicitly |
| Quote / precision | pUSD; price precision mostly 2 dp; max 5 significant figures for price and quantity |

Liquidity snapshot (24h volume from `/v1/info/statistics` and open interest from
tickers, sampled Monday 2026-10-19 10:43 UTC, before the US open). Total equity
24h volume: ~$17.2M of ~$50M exchange-wide.

| Symbol | 24h volume (pUSD) | Open interest (pUSD) |
| - | -: | -: |
| TSLA-USD | 2,363,493 | 655,969 |
| ASML-USD | 1,794,091 | 384,829 |
| SKHY-USD | 1,415,797 | 201,041 |
| MU-USD | 1,166,876 | 380,576 |
| SNDK-USD | 1,127,252 | 299,178 |
| NBIS-USD | 1,058,764 | 85,146 |
| SKHYNIX-USD | 873,410 | 334,191 |
| NVDA-USD | 741,112 | 703,844 |
| GOOG-USD | 626,497 | 727,345 |
| MSTR-USD | 574,956 | 468,433 |
| LITE-USD | 524,776 | 111,770 |
| CRWV-USD | 441,174 | 161,663 |
| ZM-USD | 425,323 | 41,811 |
| HOOD-USD | 387,803 | 231,690 |
| CXMT-USD | 335,946 | 10,759 |
| QCOM-USD | 330,019 | 100,243 |
| SAMSUNG-USD | 318,112 | 194,774 |
| META-USD | 316,407 | 415,043 |
| INTC-USD | 314,544 | 231,539 |
| ORCL-USD | 285,194 | 83,868 |
| UNITREE-USD | 280,490 | 27,854 |
| AMD-USD | 279,033 | 315,063 |
| ARM-USD | 196,838 | 285,597 |
| TSM-USD | 178,674 | 288,637 |
| PLTR-USD | 126,628 | 170,111 |
| AAPL-USD | 126,205 | 280,863 |
| CBRS-USD | 119,117 | 126,940 |
| SPCX-USD | 109,220 | 492,973 |
| RKLB-USD | 89,854 | 417,616 |
| CRCL-USD | 54,531 | 360,414 |
| AVGO-USD | 41,443 | 443,497 |
| MSFT-USD | 36,039 | 269,517 |
| AMZN-USD | 27,040 | 307,835 |
| DELL-USD | 23,716 | 11,105 |
| MRVL-USD | 18,232 | 58,185 |
| BABA-USD | 11,872 | 51,231 |
| COIN-USD | 7,320 | 87,703 |
| STRC-USD | 6,288 | 122,853 |

Book snapshot at the same time: top-of-book spreads of 0.6–4 bp on
NVDA/TSLA/GOOG/AAPL/COIN, with several $M quoted within ±10 bp on the large names.
Quoted depth far exceeds traded volume, so treat it as market-maker quotes that
can be pulled, not guaranteed liquidity. A single snapshot outside US hours is
not representative. Phase 2 should record books across sessions before sizing
assumptions are fixed.

---

## 6. Prices and market sessions

* **Index price:** weighted average of external feeds (Pyth, Chainlink Data
  Streams, Hyperliquid), with stale and outlier samples dropped. Published every
  200 ms. `GET /v1/info/index` returned an empty `constituents` list, so feed
  composition is not visible.
* **Mark price:** `median(C1, C2, C3)` every 200 ms, where
  C1 = Index + EMA₁₅₀ₛ(book mid − Index), C2 = median(best bid, best ask, recent
  last trade), C3 = external mark feeds. Each falls back to Index. **Mark drives
  margin, PnL, liquidation and TP/SL triggers**, not last trade.
* **Sessions** (Regular, Premarket, Postmarket, Overnight, Weekend, Closed,
  Halted, Unknown) change **only** which feed set prices Index and Mark. Funding,
  margin, tiers, matching and liquidation are identical around the clock.
* **The session category is not exposed by the API.** The bot must derive it
  from an exchange calendar (NYSE/Nasdaq hours, holidays, half-days) for each
  underlying. Some underlyings (SKHYNIX, SAMSUNG, CXMT, UNITREE) trade on
  non-US exchanges.
* Risk this creates: on weekends and overnight, Index and Mark may rest on thin
  feeds while the book keeps trading. Gaps at the Monday or regular-session open
  move Mark, and with it liquidations, instantly.

---

## 7. Funding

* The premium index is sampled every 5 s by walking $1,000 of notional on each side of
  the book: `IPD = max(bid_impact − Index, 0) − max(Index − ask_impact, 0)`,
  `P = IPD / Index`.
* Hourly: `F_8h = scale × (mean_P + clamp(0.0001 − mean_P, ±0.0005))`,
  `FR_hour = clamp(F_8h / 8, ±0.04)`. **`scale = 0.5` for non-crypto (stocks).**
* Settled at the end of each 1h window against every open position. Longs pay
  when the rate is positive. The protocol takes no cut.
* With zero premium, the baseline rate is `0.5 × 0.0001 / 8 =` **0.000625%/h**
  (≈ 5.5%/yr paid by longs to shorts). This matches the live rate observed
  for NVDA (`0.00000625`), which validates the formula.
* History: `GET /v1/info/funding` (100 per page, newest first); private per-account
  charges via `GET /v1/account/funding`.

---

## 8. Fees

Per fill: `fee = |price × qty| × rate`, in pUSD. Tier by trailing 30-day volume
(the changelog also mentions 14-day volume for group aggregation, so read the
tier from `portfolio.fee_tier`):

| 30d volume ≥ | Taker | Maker |
| - | - | - |
| $0 | 4.00 bp | 1.25 bp |
| $1M | 3.70 bp | 1.00 bp |
| $5M | 3.50 bp | 0.80 bp |
| $25M | 3.00 bp | 0.50 bp |
| $100M | 2.70 bp | 0.20 bp |
| $500M | 2.50 bp | 0.00 bp |
| $1B | 2.00 bp | −0.50 bp (rebate) |

Implication: a taker round trip at tier 0 costs **8 bp + spread + slippage**.
Any strategy must clear roughly 10–15 bp per round trip before funding. Fills
report `pnl` before fees; subtract `total_fee` for the net result.

---

## 9. Margin and liquidation

* `Equity = Collateral + UnrealizedPnL(Mark) − FeesDue − FundingDue`.
* `IM = Notional / Leverage` (configured leverage, applied to the whole position);
  `MM = Notional × 0.5 / max_leverage` (flat 5% on stock perps).
* States: Healthy (`Equity ≥ IM`), Margin call (`MM ≤ Equity < IM`, reduce-only),
  Liquidation (`Equity < MM`).
* **Pre-trade check uses worst-case size**:
  `max(|pos + open buys|, |pos − open sells|)`. Resting orders consume margin.
  Reversals are margined at the worst executable price (changelog, 2026-09-21).
* Liquidation: reduce-only IOC market orders that sweep the book with no
  protective spread, plus a 0.5% fee per fill. Cross liquidation unwinds one
  position at a time and blocks new orders account-wide. Severe breaches (equity
  below ~⅔ MM) go straight to the insurance fund. If the fund can't absorb them, the
  system auto-deleverages the most profitable, most leveraged opposite-side
  traders (`adl: true` fills).
* Withdrawals must leave `max(reserved collateral, 10% of open notional)`.
* Design implication: prefer **isolated margin per position**, so one bad stock
  cannot liquidate the book. Run well below 10x, since at 10x a 5% adverse move in
  Mark reaches maintenance.

---

## 10. Order capabilities

| Capability | Notes |
| - | - |
| Time in force | GTC, IOC, FOK. Post-only on GTC only. |
| Market-style orders | IOC/FOK without price (bounded by the ±10% price band). |
| Reduce-only | Yes. Resting reduce-only orders share the position as a closing budget. |
| TP/SL | Bracket on entry (dormant until the entry fully fills) or position-level (one TP and one SL per instrument, market close only). **Triggered by Mark.** Not modifiable: cancel and recreate, which is not atomic. |
| Modify | Price and total quantity of a standalone resting GTC. Not yet available in the Python SDK ("coming soon"); raw API only. |
| Cancel | By order ID, client order ID (32-char lowercase hex), batch, or cancel-all (optionally per instrument). |
| Dead man's switch | `autoCancel` deadline ≥ 5 s ahead; re-arm on an interval; daily trigger limit (1000). Disarm on clean shutdown. |
| Self-trade prevention | Always on (cancel the resting maker). |
| Not yet enabled | TWAP entry, chase limits, GTD, partial TP/SL, trailing stops, builder codes. |

Order statuses to handle (strict parsers will break otherwise): `open`, `filled`,
`ioc_no_fill`, `ioc_expired`, `fok_unfilled`, `post_only_rejected`,
`stp_cancelled`, `reduce_only_invalid`, `reduce_only_expired`,
`sweep_cap_exceeded`, `resting_order_limit_exceeded`, `below_min_notional`,
`instrument_disabled`, `instrument_close_only`, `instrument_settled`,
`insufficient_margin_at_fill`, `mark_price_unavailable`.

---

## 11. Market data and history (for backtesting)

| Data | Endpoint / channel | Depth available |
| - | - | - |
| Candles | `GET /v1/info/klines` (1000/request) · WS `klines::<id>::<iv>` (1s–1w) | Since listing. NVDA 1m/1h/1d from **2026-07-20** (~3 months). |
| Mark price | `GET /v1/info/mark-history` (down to 1s buckets) | Since listing. |
| Funding rates | `GET /v1/info/funding` (100/page) | Paginated history. |
| Public trades | `GET /v1/info/trades` (100/request) · WS `trades::<id>` | Recent tape. Deep history not confirmed. |
| Order book | `GET /v1/info/book` (depth 10/100/500/1000) · WS `book::<id>`, `bbo::<id>` | **Live only, no history.** |
| Tickers / stats | `GET /v1/info/tickers`, `/statistics` · WS `tickers::*`, `statistics::*` | Live. Tickers can be up to 10 s stale (503 if not). |

Implications:

* Three months of perp history is too short for statistically meaningful
  validation of most strategies. The backtester should combine **external
  underlying stock data** (years of OHLCV for signal research) with
  **perp-specific data** (funding, basis vs index, fees) for execution realism.
* There is no historical order book, so we must **start recording** books, BBO, trades and
  funding ourselves early (Phase 2), so that later simulations can replay real
  microstructure.

---

## 12. Rate limits

* IP: 1,000 weighted tokens/min for HTTP (book depth 100 = 5, unfiltered
  open-orders = 20, etc.). `429` / `ip_rate_limited`.
* Account actions: limit tier based on 14-day volume. Live `limit-tiers` shows the
  lowest tier at 2,000 actions/min and 1,000 open orders; the docs say 5,000. Read
  `GET /v1/account/limits` at runtime.
* WebSocket: 50 connections per IP, 100 subscriptions per connection, 1,000
  inbound messages/min per IP.

---

## 13. Operational characteristics

* **Fast-moving API.** The changelog shows weekly releases (Sep 21, 27, 29, 30,
  Oct 6). Fields get added, and new required fields and statuses appear. Parsers must
  tolerate unknown fields and unknown enum values.
* **Maintenance cancel-only windows** with forced WebSocket reconnects. New orders
  are rejected with `cancel_only_mode`; `GET /v1/info/exchange` exposes
  `cancel_only`.
* **Ambiguous outcomes.** A `503`/`500` does not prove a request didn't execute.
  Reads and idempotent updates can be retried; signed balance changes must be
  resent byte-for-byte (same signature, salt and ts). Orders reconcile via client
  order IDs.
* **State sync.** Every WebSocket frame carries a per-channel `sq`. On a gap or
  reconnect, re-snapshot (balances, open orders, orders, fills, portfolio) before
  trusting local state. Ping every ~25 s; treat the connection as stale after
  ~65 s of silence.
* Record the `x-pmp-pod` header and the auth-ack `pod` for support correlation.

---

## 14. Official Python SDK assessment (`polymarket-client`)

* Version 0.13.0 (2026-10-09), MIT, Python ≥ 3.11. Dependencies: `eth-account`,
  `httpx`, `msgpack`, `pydantic` v2, `websockets`. Async API
  (`AsyncPublicClient`, `AsyncSecureClient`, `open_perps_session()`), typed
  models with `Decimal`, built-in pagination, stream reconnect and resync events.
* All Perps methods are marked **"Experimental: may change in a breaking way in
  any release, including patch releases."** So we pin an exact version and wrap
  the SDK behind our own exchange adapter interface.
* Its signing implementation (msgpack → keccak → EIP-712 `Op`) matches the docs.
* **Bug found:** the API ignores `instrument_id` on `GET /v1/info/tickers` and
  always returns all instruments. `fetch_perps_ticker(instrument_id=X)` returns
  `tickers[0]`, which is **SP500, not X**. Verified live: requesting NVDA returned a mark of
  7700. Our adapter must filter tickers by `instrument_id` itself. This shows why
  every SDK call we rely on needs a contract test.
* Smoke test: public REST (instruments, book, registration) and a public
  WebSocket subscription (≈90 BBO/ticker events in 8 s for one instrument) worked
  from this environment.

---

## 15. Docs vs live discrepancies

| Topic | Docs | Live |
| - | - | - |
| Max leverage | FAQ: 20x for SP500/crypto, 10x RWA | SP500, NAS100, BTC, ETH: 50x. Equities: 10x. |
| Symbols | Examples use `BTC-PERP` | Live symbols are `BTC-USD`, `NVDA-USD`, ... |
| Quote asset | `USD` in examples | `pUSD` |
| Action rate limit | 5,000/min default | Lowest tier 2,000/min |
| Ticker `instrument_id` filter | Documented as a filter | Ignored by the server |
| `GET /v1/info/collateral` | Listed as "Get Collateral Assets" | 404. Path is `/v1/info/assets`. |

Rule: never hard-code venue parameters. Load them from live endpoints at startup
and on a schedule.

---

## 16. Implications for Phase 1 (architecture inputs)

1. **Exchange adapter layer** over the pinned SDK, with our own models, contract
   tests against recorded responses, and a fake exchange for tests and simulation.
2. **Instrument registry** refreshed from `/v1/info/instruments`. All order
   validation (precision, 5 significant figures, min notional, price band, risk tiers) is done locally
   before signing.
3. **Session calendar service** (exchange hours and holidays per underlying),
   because the API doesn't expose it. The strategy and risk layers both consume it.
4. **Risk engine independent of the LLM**: per-position isolated margin, leverage
   cap well below 10x, max notional per name and in total, daily loss limit,
   liquidation-distance floor, session-aware limits (tighter on weekends,
   overnight and around earnings), kill switch, and the dead man's switch always
   armed while orders rest.
5. **State reconciler** driven by WebSocket events with `sq` gap detection and REST
   re-snapshots. Client order IDs on every order for idempotency.
6. **Data recorder** (books, BBO, trades, tickers, funding) running from Phase 2
   onward, to build the simulation dataset.
7. **Simulator** modelling fees, funding (formula above), the mark/index split,
   mark-triggered TP/SL, maintenance margin and liquidation (with its fee), and queue
   and slippage effects from recorded books.
8. **LLM agent boundary**: the agent proposes structured trade intents; the risk
   engine and execution layer accept, resize or reject them. The agent never signs or
   submits directly.
9. **Live-trading gate**: off by default; requires explicit config plus a
   jurisdiction attestation; refuses to run in CI and dev environments.
10. **Secrets:** owner private key used only for proxy creation, deposits and
    withdrawals (ideally an offline/manual step). The bot holds only the proxy
    key and secret, which can trade but cannot withdraw. Rotate before expiry.
