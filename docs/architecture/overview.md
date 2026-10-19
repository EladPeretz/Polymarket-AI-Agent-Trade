# Architecture Overview

Status: proposed (Phase 1) · Last updated: 2026-10-19

This document describes how the Polymarket Perps stock-trading agent is built.
Venue facts referenced here (§ numbers) come from
[Phase 0 venue research](../phase-0/venue-research.md). Individual decisions are
recorded as ADRs in [`docs/adr/`](../adr/README.md). Phase gates are defined in
[go-no-go criteria](go-no-go.md).

---

## 1. Scope

| Topic | Decision |
| - | - |
| Venue | Polymarket Perps (international), equity perpetuals |
| Universe | Configurable symbol list. Initial: `NVDA-USD` |
| Trading window | 09:30–12:30 America/New_York, NYSE trading days. On half-days the window ends at the earlier of 12:30 and the session close. |
| Flat by | 12:30, retrying until a hard cutoff (12:35) and alerting if anything is still open |
| Direction | Long and short |
| Positions | At most one per symbol; no adding to a position in v1 |
| Agent | Claude, running in Claude Code |
| Agent inputs (v1) | Polymarket market data only. News and web search are later versions. |
| Funding / withdrawals | Out of scope. Owner-managed; the bot never holds the owner key. |
| Capital and risk numbers | To be set when the trading framework is built (Phase 2). The architecture only defines the *rules*; values live in config. |
| Historical data | Polymarket only, rolling ~3-month window. Strategies are expected to change every few months (ADR-0008). |

## 2. Runtime modes

One codebase, three modes. Every mode uses the same strategy, agent and risk
code; only the market-data source and the execution backend change.

| Mode | Market data | Execution | Agent | Where it runs |
| - | - | - | - | - |
| `backtest` | Historical candles/trades | Simulator | **Playbooks only** (no LLM; see §5.4) | Anywhere |
| `paper` | Live Polymarket feeds | Simulator against the live book | Claude | Claude Code cloud session, started daily by a scheduled task |
| `live` | Live Polymarket feeds | Polymarket order API | Claude | Self-hosted VPS in a permitted jurisdiction (ADR-0004) |

`live` mode refuses to start unless an explicit config flag and a jurisdiction
attestation are both set. It also refuses to start in CI, where it detects
known dev/CI environments.

## 3. Component view

```
                 ┌──────────────────────── engine process (deterministic, asyncio) ───────────────────────┐
 Polymarket WS ─►│ Market data feed ─► Market state store ◄── Session calendar                              │
 Polymarket REST►│        │                    │                                                            │
                 │        │             Signal / context builder ──► Trigger detector ──► event queue ─────┼──► Agent bridge ◄──► Claude
                 │        │                    │                                                    ▲        │      (tools)
                 │        ▼                    ▼                                                    │        │
                 │   Recorder          Risk engine ◄── trade intents ◄──────────────────────────────┴────────┼──── propose / close
                 │                         │ approved orders                                                  │
                 │                         ▼                                                                  │
                 │                Execution backend: [PaperSimulator] | [LiveGateway]                         │
                 │                         │ fills / order updates                                            │
                 │                         ▼                                                                  │
                 │                 Portfolio ledger ◄── Reconciler (live)                                     │
                 │                         │                                                                  │
                 │   Guardian (window close, kill switch, stale-data halt, dead man's switch)                 │
                 │   Journal (events, decisions, risk verdicts) ──► daily report ──► results store            │
                 └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Venue adapter (`venue/`)

* Wraps the pinned `polymarket-client` SDK behind our own interfaces:
  `MarketDataSource`, `AccountReader`, `OrderGateway`. Nothing outside `venue/`
  imports the SDK (ADR-0001).
* Converts SDK models to our domain models (`Decimal` everywhere, timezone-aware
  UTC datetimes).
* Works around known issues, e.g. it filters tickers by `instrument_id` itself
  (§14).
* Contract tests run against recorded API responses. A small set of live
  read-only smoke tests run on demand.

### 3.2 Market data and state (`market/`)

* WebSocket subscriptions per symbol: `bbo`, `book`, `trades`, `tickers`; REST for
  instruments, funding history and candles.
* Tracks the per-channel `sq`. On a gap or reconnect it re-snapshots via REST
  before marking the state fresh.
* Exposes a read-only `MarketSnapshot` (book top N, mark, index, last, funding
  rate, OI, recent trades, staleness flags).
* **Staleness is first-class.** If any required input is older than its
  threshold, the snapshot is marked stale, and the risk engine blocks new entries
  until it recovers.

### 3.3 Session calendar (`calendar/`)

* NYSE trading days, holidays and half-days, computed locally (the API does not
  expose sessions, §6).
* Provides `trading_window(date)`, `is_entry_allowed(now)` (no new entries after a
  configurable cutoff, default 12:15) and `must_be_flat(now)`.

### 3.4 Signals, playbooks and triggers (`strategy/`)

* **Features** are computed every update: opening range (first N minutes), session
  VWAP, gap vs previous close (from index/mark history), realized volatility,
  spread and depth, funding rate, distance to the opening-range levels.
* **Regime context** classifies the session as it develops: e.g. *expansion*
  (wide opening range vs recent average, large gap, high realized volatility),
  *compressed* (narrow range, low volatility), *trend* vs *rotation* (range
  extension vs repeated VWAP crosses). Regime labels are features. The agent sees
  them, and every trade and backtest result is tagged with them (ADR-0008).
* **Playbooks** are coded, parameterized trade setups (e.g. opening-range
  breakout, VWAP reversion, gap fade). Each has a precise trigger condition,
  default stop/target logic, a parameter schema, and the **regimes it is designed
  for** (a momentum playbook declares expansion/trend; it is not judged on
  compressed sessions). Playbooks are pure functions of features, so they can be
  backtested without an LLM. Each playbook has a version; any parameter or logic
  change creates a new version, and results are attributed per version.
* **Trigger detector** turns market activity into agent wake-ups:
  * a playbook condition became true,
  * a large move (configurable: N× short-horizon volatility, or X bp in Y seconds),
  * position management events (approaching stop or target, unrealized PnL
    threshold, time-in-trade limit),
  * fills, rejections and risk events,
  * a scheduled **check-in every 15 minutes**,
  * session milestones (open, entry cutoff, window close).

### 3.5 Agent bridge (`agent/`)

The agent is Claude in a Claude Code session. It never talks to the venue; it
uses a small tool surface served by the engine:

| Tool | Purpose |
| - | - |
| `next_event(timeout)` | Block until the next trigger (or timeout) and return the event with a fresh compact snapshot |
| `get_snapshot(symbol)` | Current market state, features and playbook states |
| `get_portfolio()` | Positions, open orders, PnL, remaining risk budget |
| `list_playbooks()` | Available playbooks, their current trigger state and parameter limits |
| `propose_trade(intent)` | Submit a `TradeIntent`. Returns the risk verdict (approved / resized / rejected, with reasons). |
| `adjust_protection(symbol, stop, target)` | Tighten a stop or move a target, within risk rules |
| `close_position(symbol, reason)` | Exit now |
| `journal(note)` | Record reasoning that isn't tied to an action |

`TradeIntent` schema (validated before risk):

```text
symbol, side (long|short), source (playbook:<id> | discretionary),
entry (limit price | marketable), size (fraction of the per-trade risk budget, 0–1),
stop_price (required), target_price (optional), time_stop (optional),
thesis (text), confidence (0–1)
```

The agent sizes trades in **risk units, not quantity**. The risk engine converts
a risk budget plus the stop distance into a quantity, so the agent cannot
accidentally oversize.

Transport: the tools are exposed to Claude Code as an MCP server, or as a thin CLI
the session calls. Which one is settled by a Phase 2 spike. Known constraint: a
single tool call or foreground command in a cloud session has a time cap
(currently 10 minutes for Bash), so `next_event` timeouts are kept below that. The
15-minute check-in is an engine-side timer, not a single long wait.

Context management: each event carries a compact, self-contained snapshot, so
the agent does not need to remember the whole morning. A short "session state"
summary (positions, today's trades, lessons noted) is included with every event,
so context compaction during the 3-hour run loses nothing important.

### 3.6 Autonomy model: playbooks plus bounded discretion

This is where the agent's freedom meets the framework. Rather than fixing that
line in code, it is a **config-defined autonomy envelope** we can move as
evidence accumulates (ADR-0003):

| Level | What the agent may do | Envelope |
| - | - | - |
| `L0` filter | Approve or veto playbook signals the engine generates | Playbook defaults |
| `L1` playbook | Choose a playbook, direction, size and stop/target within playbook parameter limits, or stay out | Full per-trade risk budget |
| `L2` discretionary | Propose a trade not tied to any playbook | Reduced risk budget (e.g. 50%), max N per day, wider justification required |

* Per-symbol config enables levels and sets their caps.
* Every trade is tagged with its source (`L0`/`L1`/`L2`, playbook id), so paper
  results show **separately** whether the playbooks, the agent's selection, or
  the agent's discretion add value. That data is what tells us where the line
  should move.
* Risk rules (§3.7) apply identically to every level. Autonomy changes what the
  agent may propose, never what the risk engine allows.

### 3.7 Risk engine (`risk/`)

Deterministic and independent of the agent (ADR-0002). The agent can read the remaining
budget but cannot change any parameter. Every intent passes all checks or is
rejected (or resized down, never up).

Pre-trade checks (values set in config; capital-related values decided in Phase 2):

1. Trading window open and before the entry cutoff; symbol enabled; instrument
   not `close_only`.
2. Market snapshot fresh; spread ≤ max; top-of-book depth ≥ min.
3. Stop present, on the correct side, and at least a minimum distance away
   (noise floor); target, if present, on the correct side.
4. Size from risk: `qty = risk_budget × size_fraction / |entry − stop|`, rounded
   down to venue precision; notional ≥ venue min ($10); ≤ max notional per position.
5. Leverage ≤ configured cap (well below the venue's 10x); isolated margin.
6. **Liquidation buffer:** the stop must sit well inside the estimated
   liquidation price (MM = 5% of notional on stock perps, §9), so the stop always
   triggers before liquidation could.
7. One position per symbol; max concurrent positions; max trades per day; max
   L2 trades per day.
8. Daily loss limit: when realized + unrealized PnL hits it, **halt new entries
   for the day** (exits still allowed).
9. Venue order validation: price/quantity precision, 5 significant figures, ±10%
   price band, min notional (§5).

Continuous checks: stale data halts entries; a position without a confirmed stop
gets an immediate protective stop or is closed.

### 3.8 Execution (`execution/`)

* Entries: limit orders by default (optionally post-only), with an entry timeout
  after which unfilled quantity is cancelled. Marketable entries are allowed only
  when the intent asks and the spread check passes.
* Exits: marketable reduce-only IOC with a price limit (protects against a thin book).
* **Protection:** every position gets a stop-loss from the moment it opens.
  In live mode it is the exchange-side position TP/SL (mark-triggered, §10), so
  protection survives a crash of the agent, the engine or the session. In paper
  mode the simulator emulates the same mark-triggered behaviour.
* Client order IDs (32-hex) on every order for idempotency and reconciliation.
* Live mode keeps the exchange **dead man's switch** armed while any order rests,
  re-arming every 20 s with a 60 s deadline, and disarms it on clean shutdown.

### 3.9 Paper simulator (`sim/`)

There is no Perps testnet, so paper trading uses our own fill model fed by live
data (ADR-0005). The fill model is deliberately conservative:

| Event | Model |
| - | - |
| Marketable order | Walks the live book at decision time plus a latency allowance. Fills at the level prices, with partial fills if depth is insufficient. |
| Resting limit | Fills only when a public trade prints **through** the limit price (touching it is not enough). No queue position is assumed. |
| Fees | Account's tier rates (tier 0: taker 4 bp, maker 1.25 bp) |
| Funding | Live hourly funding rate applied to open positions at each settlement |
| Stops/targets | Triggered by **live mark price**, filled as marketable orders against the book |
| Liquidation | Modelled from mark vs maintenance margin as a safety assertion. In normal operation it must never happen. |

The same simulator runs backtests from historical data, with a reduced model
(candles/trades instead of books).

### 3.10 Guardian (`guardian/`)

An independent supervisor task inside the engine, with no LLM dependency:

* **Window close:** from 12:30 it cancels all orders and closes all positions,
  retrying until 12:35, then alerts and records an incident.
* **Kill switch:** a config flag or file that, when set, cancels everything,
  closes positions and stops new entries immediately.
* **Agent liveness:** if the agent hasn't responded to events for a configurable
  period, it blocks new entries. Existing positions stay protected by their
  stops and are closed at window end as usual.
* **Engine liveness (live):** exchange-side stops plus the dead man's switch
  protect the account even if the whole process dies.

### 3.11 Ledger, reconciler and journal (`ledger/`, `journal/`)

* Event-sourced ledger: every order, fill, funding payment and fee is recorded as
  an event. Positions and PnL are derived from it.
* Live mode: the reconciler compares the ledger against exchange state
  (portfolio, open orders, fills) at startup, after every reconnect, and every few
  minutes. Any mismatch blocks new entries and raises an incident (a go/no-go
  metric).
* The decision journal records, for each agent event: the event, the snapshot
  hash, the agent's tool calls and thesis, the risk verdict, and the outcome.

### 3.12 Results store

Cloud sessions are temporary, so at the end of each session the run commits
`results/<date>/` (event log, journal, daily report) to a dedicated
**`paper-results` branch** (ADR-0006). The daily report covers trades, PnL
(gross, fees, funding, net), per-autonomy-level attribution, risk rejections,
incidents, and agent latency.

## 4. Daily lifecycle (paper mode)

| Time (New York) | Step |
| - | - |
| 09:15 | Scheduled task starts a cloud session. The session installs deps, loads config, checks the calendar (exits on non-trading days) and starts the engine in the background. |
| 09:15–09:30 | Engine connects feeds, builds pre-open context (prior close, overnight move, funding). Agent receives a `session_start` briefing. |
| 09:30–12:15 | Agent loop: `next_event` → reason → act or pass. Engine runs triggers, risk, simulator and guardian continuously. |
| 12:15 | Entry cutoff: only exits and protection adjustments allowed. |
| 12:30–12:35 | Guardian closes everything. |
| ~12:40 | Daily report generated, results committed to `paper-results`, session ends. |

Live mode (VPS) follows the same lifecycle, with the engine and a headless Claude
Code session started by the server's scheduler.

## 5. Testing and evaluation strategy

1. **Unit tests**: features, playbooks, risk rules, sizing, calendar, simulator
   fill model (pytest).
2. **Contract tests**: the venue adapter against recorded API/WebSocket fixtures,
   plus regression tests for known SDK issues.
3. **Simulation tests**: scripted market scenarios (gap, spike, stale feed,
   reconnect, partial fills) driving the full engine with a scripted fake agent,
   asserting that risk and guardian rules hold.
4. **Playbook backtests** (no LLM): a rolling ~3-month window of Polymarket data,
   segmented by regime, with fees and funding. They are a **sanity screen, not
   proof**: they check that a playbook behaves as designed in the regimes it targets
   and doesn't bleed in them. Re-run on every playbook version and on a regular
   cadence as the window rolls (ADR-0008).
5. **LLM evaluation is forward-only.** Backtesting Claude on past dates is not
   meaningful, because the model may already know what happened on those dates.
   The agent's value is measured in paper trading, attributed per autonomy level.

## 6. Repository layout (target)

```
src/perps_agent/
  venue/        SDK adapter, domain models, venue-rule validation
  market/       feeds, state store, snapshots
  calendar/     NYSE sessions and trading window
  strategy/     features, playbooks, trigger detector
  agent/        tool server (MCP/CLI), TradeIntent schema, prompts
  risk/         risk engine and sizing
  execution/    order manager, protection, dead man's switch
  sim/          paper/backtest simulator
  guardian/     window close, kill switch, liveness
  ledger/       event-sourced ledger, reconciler
  journal/      decision journal, daily report
  app/          entry points: run (paper|live), backtest, report
config/         default.yaml, symbols/NVDA-USD.yaml (no secrets)
tests/          unit/, contract/, simulation/
docs/           phase docs, architecture, ADRs
scripts/        one-off tools (e.g. connectivity check)
```

## 7. Security and secrets

* The bot holds only the Perps **proxy** key and secret, which can trade but
  cannot withdraw. The owner key never enters any environment the bot runs in.
* Paper mode needs **no credentials at all**, because public market data is enough.
* Secrets come from environment variables (cloud environment secrets / VPS
  secret store), never from files in the repo. `.env` is git-ignored.
* Logs and journals never contain secrets; a test enforces redaction.

## 8. Phase 2 work packages (one PR each)

1. Project skeleton: package layout, config loading, CI (lint, format, type
   check, tests), `CLAUDE.md` conventions.
2. Venue adapter + domain models + contract tests.
3. Market data feed + state store + recorder.
4. Session calendar.
5. Simulator (fill model, fees, funding, mark-triggered stops).
6. Risk engine + sizing.
7. Execution + ledger (paper backend).
8. Features, playbooks (first: opening-range breakout) and trigger detector.
9. Agent bridge (tools, `TradeIntent`, prompts), including the MCP-vs-CLI transport spike.
10. Guardian + daily report + results branch publishing.
11. Daily scheduling in Claude Code cloud + a dry-run session.
12. Backtest runner for playbooks.

Live gateway, reconciler and dead man's switch follow once paper trading meets
its gate, before the VPS move.
