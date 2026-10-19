# Phase 0 — Open Questions and Gaps

Items that block or shape Phase 1. Each needs an answer from the project owner or
a check against the live account. See [venue-research.md](venue-research.md) for
the background on each one.

## Decisions so far (2026-10-19)

| Topic | Decision |
| - | - |
| Trading universe | Configurable list of symbols. Initial list: `NVDA-USD`. |
| Trading window | First 3 hours after the US regular-session open (09:30–12:30 America/New_York), exchange trading days only. |
| LLM | Claude. |
| Funding and withdrawals | Out of scope for the bot. The owner handles them manually; the bot never holds the owner key. |
| Live-trading location | Unknown. Build with the live-trading gate off until it is decided (B1). |
| Capital, drawdown, leverage | Deferred to Phase 1 (architecture). |

## Blocking

| # | Question | Why it matters |
| - | - | - |
| B1 | Which jurisdiction will live trading run from (operator location **and** server location)? | Order placement is prohibited from the US, Canada and others (§4). This decides where the live process runs, and whether live trading is possible at all. |
| B2 | Is the existing wallet's EOA already registered for Perps (`GET /v1/info/registered?address=<EOA>`)? If not, is an invite code needed? | The first `createProxy` registers the account. An invite/referral `code` field exists but its necessity is undocumented. **2026-10-19:** neither supplied address (`0x20a4…f12d`, `0xD3Cf…e7Ba`) is registered, so no Perps account exists yet. The account is email-based, so the signing EOA is the email-login wallet. Registration happens on first Perps onboarding (owner action). |

## Needed before Phase 1 sign-off

| # | Question | Default if unanswered |
| - | - | - |
| Q1 | Wallet type of the existing account: plain EOA, Safe, or Polymarket proxy wallet? | Assume Safe/proxy. Perps is still keyed by the EOA. |
| Q2 | Starting capital for the Perps account and maximum acceptable drawdown? | Size simulations for $1k–$10k with a 10% max drawdown. |
| Q3 | Leverage ceiling (venue max for stocks is 10x)? | 3x per position, isolated margin. |
| Q4 | Trading universe: all 38 stock perps, a liquid subset, or US-listed only? | Top ~10 by 24h volume, US-listed underlyings only (simpler session calendar). |
| Q5 | Holding horizon: intraday, multi-day swing, or both? | Intraday-to-few-days. That decides whether funding or fees dominate cost. |
| Q6 | Trade outside US regular hours (overnight/weekend books keep trading on thinner feeds)? | Regular + extended hours only. Flat or reduced exposure on weekends. |
| Q7 | Which LLM provider and model, and what monthly inference budget? | Claude via the Anthropic API, with costs capped in config. |
| Q8 | Where does the bot run in production (VPS region, always-on)? | Depends on B1. |
| Q9 | Source for long-history underlying stock data (free vs paid: e.g. Polygon.io, Alpaca, Tiingo, yfinance)? | Start free, abstract behind a data-source interface. |

## Known gaps in the venue (to design around, not to answer)

* No API for the current market-session category, so we need our own exchange calendar.
* No historical order books, so recording must start early.
* Only ~3 months of perp history, so signal research needs external data.
* Feed composition behind Index is not visible (`constituents` is empty).
* Perps SDK APIs are "experimental", so pin the version and add contract tests.
  One real bug has already been found: the single-ticker lookup returns the wrong instrument.
* Order modify is not in the Python SDK yet (raw API only).
* No public endpoint reports the terminal outcome of an ambiguous signed
  isolated-margin adjustment. Avoid automated margin adjustments, or reconcile carefully.
