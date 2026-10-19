# Go / No-Go Criteria and Rollback

Status: proposed (Phase 1) · Last updated: 2026-10-19

Each phase ends with an explicit decision: **merge and continue**, or **fall back**
to the previous tagged version. The criteria are fixed here, before results exist,
so the decision is objective. Values marked *TBD* depend on capital and are set
when the trading framework is built. They must be set before the gate that uses them.

## Gate 1 — Playbook screen (before a playbook version enters paper trading)

Backtesting here is a screen, not proof (ADR-0008). History is short (rolling
~3 months of Polymarket data) and strategies are expected to change every few
months. So the gate asks "does this playbook version behave as designed **in the
market conditions it is built for**?", not "is it profitable on all history?".

For each new playbook version, over the rolling window, **restricted to sessions
whose regime matches the playbook's declared regimes** (e.g. a momentum playbook is
not judged on compressed sessions):

* the trigger fires on the setups it describes (reviewed sample of trades/charts),
* net expectancy after fees and funding is not negative,
* no single trade accounts for more than 25% of net PnL,
* in non-target regimes the playbook either does not fire or is gated off by its
  regime filter.

The result is recorded with the playbook version and the regime breakdown. A
version that fails stays out of paper trading, or runs at `L0` (filter) only.
Trade counts are reported, not required: a thin sample is acceptable because paper
trading is the main evidence.

## Gate 2 — Paper trading → live pilot (end of Phase 3 simulation)

All of the following, measured on paper trading with the live data feed:

| Criterion | Threshold |
| - | - |
| Duration | ≥ 20 trading days (playbook/parameter changes don't reset the count; changes to risk, execution or guardian code restart the safety criteria) |
| Net PnL after fees and funding | > 0 |
| Profit factor (gross wins ÷ gross losses) | > 1.2 |
| Maximum drawdown | < *TBD* % of paper equity |
| Risk-rule violations (an order reached execution that should have been rejected) | **0** |
| Unexplained ledger mismatches / unreconciled state | **0** |
| Window close-outs completed by 12:35 | 100% of days |
| Sessions completed without manual intervention | ≥ 95% of trading days |

Also reported, but not gating: results per autonomy level (`L0`/`L1`/`L2`), per playbook version and per regime, win
rate, average win/loss, average agent latency per event, and LLM usage.

## Gate 3 — Live pilot → normal live operation

Run on the VPS with small capital (*TBD*) for ≥ 10 trading days:

* all Gate 2 safety criteria hold (0 violations, 0 mismatches, 100% close-outs),
* live fills and slippage within a *TBD* tolerance of the paper simulator's
  model for comparable trades (validates the simulator),
* no venue-side incident (rejected orders from validation errors, auth
  failures, geo blocks).

## Rollback procedure

* Every phase ends with a version tag on `main` (`v0.1.0` = Phase 1,
  `v0.2.0` = Phase 2, …).
* **No-go** at a gate → the phase's PR is not merged (or is reverted). Production
  (paper or live) keeps running from the last tag that passed its gate.
* Any of these triggers an **immediate stop** (kill switch, flatten) and a review
  before trading resumes: a risk-rule violation, an unexplained ledger mismatch,
  a missed window close-out, or a live loss beyond the daily limit.
