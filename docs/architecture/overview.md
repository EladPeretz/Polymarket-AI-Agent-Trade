# Architecture Overview

Status: proposed (Phase 1) · Last updated: 2026-10-20

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
| Universe | Configurable symbol list. Initial: `NVDA-USD`, `TSLA-USD`, `MSFT-USD`, `HOOD-USD`, `MSTR-USD`, `WTIOIL-USD` |
| Trading window | 09:30–12:30 America/New_York, NYSE trading days. On half-days the window ends at the earlier of 12:30 and the session close. |
| Flat by | 12:30, retrying until a hard cutoff (12:35) and alerting if anything is still open |
| Direction | Long and short |
| Positions | At most one position per symbol. The agent may add to a winning position and manage it actively (partial exits, trailing stops, runners) within risk rules (ADR-0009). |
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
* Mark-price history and exchange status are not in the SDK; the adapter reads
  them with plain HTTP GETs, still inside `venue/`.
* Local venue-rule checks (precision, 5 significant figures, price band,
  notional limits) are pure functions in `venue/rules.py`, reused by the risk
  engine.
* An in-memory fake exchange (`venue/fake.py`) implements the market-data and
  order interfaces for tests. It never signs or sends anything.
* Contract tests run against recorded API responses
  (`scripts/record_venue_fixtures.py`). A small set of live read-only smoke
  tests run on demand (`PERPS_AGENT_LIVE_TESTS=1 pytest tests/live`).

### 3.2 Market data and state (`market/`)

* WebSocket subscriptions per symbol: `bbo`, `book`, `trades`, `tickers`; REST for
  instruments, funding history and candles.
* `sq` is the engine-wide sequence, so it only orders frames within a channel;
  jumps are normal and do not signal a gap. The SDK reconnects silently, so
  staleness is judged by time: a channel that goes quiet for longer than its
  threshold is re-snapshotted via REST before the state is marked fresh.
* Book and ticker frames arrive every 100 ms, so they are the required channels:
  either one silent, or carrying a venue timestamp older than
  `market.stale_after_ms` (2 s), marks the instrument stale. BBO and trades only
  arrive on change and are never required. After a gap, and at startup, the
  feed re-snapshots book, ticker and the missed trades via REST once the stream
  is live again; only then is the instrument fresh. No frame at all for
  `market.resubscribe_after_ms` closes and reopens the subscription.
* Exposes a read-only `MarketSnapshot` (book top N, mark, index, last, funding
  rate, OI, recent trades, staleness flags).
* **Staleness is first-class.** If any required input is older than its
  threshold, the snapshot is marked stale, and the risk engine blocks new entries
  until it recovers.
* The **recorder** appends every stream event, REST re-snapshot and feed status
  change (subscribed, stale, fresh, resubscribing, stopped) to
  `data/recordings/<UTC date>/<SYMBOL>.jsonl`. Repeated book and ticker frames
  are written once. Recordings stay local (ADR-0006); the simulator and
  backtests replay them with `read_recording`. `perps-agent record --seconds N`
  runs the feed and recorder on their own.

### 3.3 Session calendar (`calendar/`)

* NYSE trading days, holidays and half-days, computed locally from the exchange
  rules (the API does not expose sessions, §6). The rules are verified for
  2022–2030 against NYSE's published lists; other years raise rather than guess.
  Unscheduled closures (e.g. a day of mourning) go in `SPECIAL_CLOSURES` or
  `session.extra_closures` in the config.
* Provides `trading_window(date)`, `is_entry_allowed(now)` (no new entries after a
  configurable cutoff, default 12:15) and `must_be_flat(now)`, which is true
  whenever `now` is outside the window, so positions only exist inside it. On
  13:00 early-close days the window is capped at the close.
* `session_at(now)` labels the underlying's session (premarket, regular,
  postmarket, overnight, weekend, closed) for session-aware features and limits.
* `perps-agent calendar [--date YYYY-MM-DD]` prints the day's window; it exits 3
  on a non-trading day, so the daily scheduled session can stop cleanly.

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
| `adjust_protection(symbol, stop, target)` | Tighten a stop or move/remove a target, within risk rules |
| `update_exit_plan(symbol, plan)` | Replace the position's exit plan (scale-outs, breakeven rule, trailing rule) at any time |
| `set_trailing_stop(symbol, rule)` | Start or change the trailing rule the engine applies continuously |
| `add_to_position(symbol, intent)` | Add size to a profitable position (same risk checks as a new trade, plus §3.7 rule 10) |
| `reduce_position(symbol, fraction, reason)` | Partial exit |
| `close_position(symbol, reason)` | Exit now |
| `journal(note)` | Record reasoning that isn't tied to an action |

`TradeIntent` schema (validated before risk):

```text
symbol, side (long|short), source (playbook:<id> | discretionary),
entry (limit price | marketable), size (fraction of the per-trade risk budget, 0–1),
stop_price (required), target_price (optional), time_stop (optional),
exit_plan (optional: scale-out steps, breakeven trigger, trailing rule),
thesis (text), confidence (0–1)
```

Exit plans and trailing rules are chosen by the agent case by case; the
engine provides a vocabulary of rule types (volatility-based distance,
structure-based such as swing lows/highs or opening-range levels, VWAP-based,
fixed distance, breakeven-after-R) that the agent combines and parameterizes.

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
   (noise floor); target, if present, on the correct side and worth at least
   `min_reward_risk` (2R) after costs, measured from the final entry price.
   A trade without a target is allowed (a runner managed by its stop). The rule
   applies at entry; targets may still be moved afterwards.
4. Size from risk: `qty = risk_budget × size_fraction / loss per unit`, rounded
   down to venue precision, where the loss per unit is `|entry − stop|` plus the
   entry fee and the stop's taker fee and slippage; notional ≥ venue min ($10);
   ≤ max notional per position.
5. Leverage ≤ configured cap (at most the instrument's venue max); isolated margin.
6. **Liquidation buffer:** the stop must sit well inside the estimated
   liquidation price (MM = 5% of notional on stock perps, §9), so the stop always
   triggers before liquidation could.
7. One position per symbol; max concurrent positions; max trades per day; max
   L2 trades per day.
8. Daily loss limit, a **hard cap on the day's worst case**: when realized +
   unrealized PnL hits it, halt new entries for the day (exits still allowed).
   Before that, each new budget is cut to the room left under the limit after
   what open positions and working entries would lose at their stops, costs
   included, so the day stays within the limit if every stop fills.
9. Venue order validation: price/quantity precision, 5 significant figures, ±10%
   price band, min notional (§5).
10. **Adding to a position** is allowed only while the position is in profit, and
    only if the combined risk (all entries to the current stop) stays within the
    position's approved risk budget. In practice the stop must already protect the
    earlier entries.
11. **Stops only tighten.** Any stop change (manual or trailing) that would move the
    stop against the position, or remove it, is rejected. Runners are let run by
    not taking profit, never by widening risk.

Continuous checks: stale data halts entries; a position without a confirmed stop
gets an immediate protective stop or is closed. The 12:30 close-out applies to
runners with no exceptions.

Implementation (`risk/`, WP6). `RiskEngine.evaluate(intent, instrument,
snapshot, state, now)` returns a `RiskVerdict`: approved, resized or rejected,
with every failed check listed at once, and the exact `OrderRequest` (fresh
32-hex client order id) that execution may send. `evaluate_add` applies rule 10
and `check_stop_change` rule 11. The engine refuses to construct while any risk
value is `null`.

* The budget is `per_trade_risk_fraction` of the lower of current and
  start-of-day equity (× `l2_risk_fraction` for L2), times the intent's `size`,
  and at most `daily_loss_room` (rule 8).
* Costs are part of the risk: the entry fee (maker only for post-only entries,
  taker otherwise), and the stop's taker fee plus `stop_slippage_bp`. Fee rates
  come from the account tier (`FeeRates`), so a stop-out loses the budget, not
  the budget plus costs.
* A marketable entry is priced at the worst book level its size reaches, sized
  over that longer distance, and sent as an IOC limited to that price. It is cut
  to the depth the snapshot shows.
* Leverage is the lowest of `max_leverage`, the venue tier cap for the position
  notional, and the highest whole leverage that keeps the stop inside the
  liquidation buffer. If even 1x fails the buffer, the intent is rejected.
* The stop is rounded away from the entry onto the venue's price grid; a limit
  entry is rounded passively.
* `RiskState` (equity, available margin, start-of-day equity, open positions with
  their approved risk, working entries, today's entry counts) is built by the
  ledger (WP7), including `pending_risk`, what working entries would lose at
  their stops. Adds count as entries toward `max_trades_per_day`.

### 3.8 Execution (`execution/`)

* Entries: limit orders by default (optionally post-only), with an entry timeout
  after which unfilled quantity is cancelled. Marketable entries are allowed only
  when the intent asks and the spread check passes.
* Exits: marketable reduce-only IOC with a price limit (protects against a thin book).
* **Protection:** every position gets a stop-loss from the moment it opens.
  In live mode it is the exchange-side position TP/SL (mark-triggered, §10), so
  protection survives a crash of the agent, the engine or the session. In paper
  mode the simulator emulates the same mark-triggered behaviour.
* **Engine-run trailing and exit plans:** the engine evaluates the active trailing
  rule and exit plan on every relevant update, independently of when the agent
  wakes. Stop moves are rate-limited and only tighten. On the venue a position
  stop is replaced by cancel-then-create, which is not atomic, so during the
  replacement the engine itself watches mark against the stop and exits if it is
  crossed.
* Client order IDs (32-hex) on every order for idempotency and reconciliation.
* Live mode keeps the exchange **dead man's switch** armed while any order rests,
  re-arming every 20 s with a 60 s deadline, and disarms it on clean shutdown.

Implementation (`execution/`, WP7, paper backend). `ExecutionEngine` drives an
`ExecutionBackend` (`SimBackend` over the simulator in paper mode) and books
everything in the ledger.

* `enter(verdict)` sends exactly the order the risk engine approved, after
  setting its leverage. A verdict older than `risk.max_snapshot_age_ms` is
  refused, so the agent re-proposes against fresh data. A limit entry still working after
  `execution.entry_timeout_seconds` is cancelled by `tick`.
* As soon as an entry or add fills, the position TP/SL is set to the approved
  stop and target and confirmed in the ledger. If that fails, the position is
  closed; `tick` also closes any position without a confirmed stop.
* `close_position`, `reduce_position` and `close_all` send reduce-only IOC
  orders limited `execution.exit_slippage_bp` through the mark. A close that
  does not fill completely is re-sent on each `tick` until flat, and closing
  cancels the symbol's working entries. Time stops close on `tick`.
* `update_protection` tightens the stop (checked by
  `RiskEngine.check_stop_change`) and moves or removes the target.
* Venue reports use the simulator's event vocabulary; orders the venue sends
  itself (TP/SL closes, liquidations) are registered from their trigger or fill.
* Not yet: engine-run trailing rules and exit plans (ADR-0009, with WP9/WP10),
  the non-atomic stop replacement watch and the dead man's switch (live only).

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

Implementation (`sim/`, WP5). `PaperSimulator` implements `OrderGateway` and
`AccountReader` and is fed every market event; each call returns the order
updates, fills, funding payments, TP/SL triggers and liquidations it caused.

* **Latency** is `sim.latency_ms` (default 250 ms): an order fills against the
  first book published at or after it reaches the venue, never the book it was
  decided on. Liquidity it takes stays taken until the next book frame.
* Market orders are bounded by the price band; unfilled IOC quantity expires, a
  FOK fills in full or not at all, a GTC rests its remainder. Post-only orders
  that would cross are rejected. A public trade's quantity is shared across our
  resting orders, best price first.
* Venue rules (`venue/rules.py`), the worst-case pre-trade margin check (§9 of
  the venue research) and reduce-only capping are applied as on the venue.
* Positions are isolated, at the instrument's max leverage unless set. Funding
  settles at the ticker's `next_funding` with the last rate and mark before it.
* Position TP/SL (one each per instrument) trigger on mark and close the whole
  position with a market order, through the same latency, sweeping each new
  book until flat.
* Fee rates are `sim.taker_fee_rate` / `sim.maker_fee_rate` (tier 0 by default).
  `sim.starting_collateral` is capital, so it is `null` until the owner sets it,
  and paper and backtest modes refuse to start without it.
* `sim.replay` replays a recording. The recorder skips repeated frames, so
  between records the last book is taken to still hold while the feed was live,
  and nothing is assumed while it was stale.
* Not modelled yet: bracket TP/SL on entry (the execution layer sets the
  position TP/SL after the fill), cross margin, order modification, and the
  candle-based backtest model (WP13).

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
* Implementation (`ledger/`, WP7): `Ledger.record` assigns a sequence number,
  applies the event and hands it to a sink (`JsonlLedgerSink` writes one JSON
  line per event); `Ledger.from_events` rebuilds the same state from the log.
  Events: `day_started`, `order_sent` (with purpose, position id and, for
  entries, the approved stop, target, risk and autonomy level), `order_failed`,
  `order_changed`, `fill`, `funding` and `protection`. The accounting matches the
  simulator's, and tests check the two agree. `ExecutionEngine.mismatches()`
  compares ledger and venue positions, the seed of the live reconciler.
* Live mode: the reconciler compares the ledger against exchange state
  (portfolio, open orders, fills) at startup, after every reconnect, and every few
  minutes. Any mismatch blocks new entries and raises an incident (a go/no-go
  metric).
* The decision journal records, for each agent event: the event, the snapshot
  hash, the agent's tool calls and thesis, the **scenarios it considered with
  their estimated probabilities**, the chosen plan and expected outcome, the risk
  verdict, and the actual outcome. Scenario records are for the learning loop
  only and are not shown in daily reports or alerts (ADR-0010).
* Implementation (`journal/`, WP8): one append-only thread of entries per
  decision, sharing a `decision_id`: `decision` (trigger, snapshot digest and
  the features used, regime and setup tags, thesis, scenarios with
  probabilities, expected R and the assumptions each depends on, the chosen
  scenario, the `TradeIntent`, tool calls), `risk_verdict`, `order_linked` (the
  ledger order and position it opened), `action_failed`, `outcome` (derived
  from the ledger when the position closes: prices, PnL after costs, R
  multiple, exit reason) and `review` (the scenario that played out and which
  assumptions held). Entries are checked against the thread, so a review can
  only judge scenarios and assumptions the decision weighed.
  `Journal.assumption_checks` returns the reviewed occurrences of an assumption
  (latest review per decision, optionally per setup or regime), the evidence
  the learning loop counts before changing how a playbook is applied (owner
  rule: at least 8 failures in the last 10). Stored as
  `results/<date>/journal.jsonl`, with known secret values redacted.

### 3.12 Results store

Cloud sessions are temporary, so at the end of each session the run commits
`results/<date>/` (event log, journal, daily report) to a dedicated
**`paper-results` branch** (ADR-0006). The daily report covers trades, PnL
(gross, fees, funding, net), per-autonomy-level attribution, risk rejections,
incidents, and agent latency.

### 3.13 Decision process and learning loop (`agent/`, ADR-0010)

* **Scenario reasoning:** before acting, the agent lays out plausible scenarios
  for the current context and regime, estimates their probabilities and outcomes
  for candidate plans, and chooses the most favourable risk-weighted idea, or
  stands aside. It can call `simulate(symbol, horizon, plan)`: the engine
  generates price paths calibrated to the current regime (recent volatility,
  intraday profile, jump behaviour) and returns the outcome distribution for the
  plan (stop/target/trail hit rates, expected R). Use of the tool is the agent's
  choice.
* **Daily review (after the close-out):** the agent compares outcomes with its
  predictions (probability calibration, thesis accuracy, management vs a
  fixed-exit baseline) and updates its **lessons notebook**, tagged by regime and
  setup, with the evidence behind each lesson.
* **Briefing:** each session starts with the lessons relevant to the day's
  context and the agent's recent calibration statistics.
* **Weekly review:** aggregates results, retires lessons the evidence no longer
  supports, and **tunes playbooks**.
* **Agent-owned strategy state:** lessons and playbook parameters/variants live
  in an agent-state store versioned on the results branch, not in `main`. The
  agent changes them itself within bounds that `main` defines. Every change
  creates a new playbook version, must pass the automated Gate 1 screen before
  use, is listed in the daily report, and can be reverted.
* **Never learnable:** risk-engine rules and values, the trading window and
  cutoffs, the guardian, the autonomy envelope, and code. These change only
  through owner-reviewed PRs.

## 4. Daily lifecycle (paper mode)

| Time (New York) | Step |
| - | - |
| 09:15 | Scheduled task starts a cloud session. The session installs deps, loads config, checks the calendar (exits on non-trading days) and starts the engine in the background. |
| 09:15–09:30 | Engine connects feeds, builds pre-open context (prior close, overnight move, funding). Agent receives a `session_start` briefing including relevant lessons and its calibration stats. |
| 09:30–12:15 | Agent loop: `next_event` → reason → act or pass. Engine runs triggers, risk, simulator and guardian continuously. |
| 12:15 | Entry cutoff: only exits and protection adjustments allowed. |
| 12:30–12:35 | Guardian closes everything. |
| ~12:40 | Daily review: the agent compares predictions with outcomes and updates lessons (and, weekly, playbooks). Daily report generated; results and agent state committed to `paper-results`; session ends. |

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
config/         default.yaml, symbols/<SYMBOL>.yaml (no secrets)
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
4. Session calendar (no dependency on 2 or 3; can run in parallel).
5. Simulator (fill model, fees, funding, mark-triggered stops).
6. Risk engine + sizing.
7. Execution + ledger (paper backend).
8. Trade journal: the append-only decision journal (§3.11) as its own package,
   built after the ledger so decisions link to real fills, and before the agent
   bridge, which writes to it. JSONL per session on `paper-results`.
9. Features, playbooks (first: opening-range breakout) and trigger detector.
10. Agent bridge (tools, `TradeIntent`, prompts), including the MCP-vs-CLI transport spike.
11. Guardian + daily report + results branch publishing.
12. Daily scheduling in Claude Code cloud + a dry-run session.
13. Backtest runner for playbooks.
14. Learning loop (§3.13): lessons notebook, daily and weekly review, briefing,
    agent-state store with its bounds defined in `main`, and the `simulate` tool.
    After the backtest runner, because playbook changes must pass Gate 1.

Live gateway, reconciler and dead man's switch follow once paper trading meets
its gate, before the VPS move.
