# Go / No-Go Criteria and Rollback

Status: proposed (Phase 1) · Last updated: 2026-10-19

Each phase ends with an explicit decision: **merge and continue**, or **fall back**
to the previous tagged version. The criteria are fixed here, before results exist,
so the decision is objective. Values marked *TBD* depend on capital and are set
when the trading framework is built. They must be set before the gate that uses them.

## Gate 1 — Playbook backtest (end of Phase 2 development)

A playbook may run in paper trading only if its backtest (fees and funding
included, walk-forward, no in-sample parameter selection) shows:

* positive net expectancy per trade after costs,
* profit factor > 1.1,
* at least 30 trades in the test period,
* no single trade contributing more than 25% of total net PnL.

Playbooks that fail stay available at `L0` (filter) only, or are dropped.

## Gate 2 — Paper trading → live pilot (end of Phase 3 simulation)

All of the following, measured on paper trading with the live data feed:

| Criterion | Threshold |
| - | - |
| Duration | ≥ 20 trading days |
| Net PnL after fees and funding | > 0 |
| Profit factor (gross wins ÷ gross losses) | > 1.2 |
| Maximum drawdown | < *TBD* % of paper equity |
| Risk-rule violations (an order reached execution that should have been rejected) | **0** |
| Unexplained ledger mismatches / unreconciled state | **0** |
| Window close-outs completed by 12:35 | 100% of days |
| Sessions completed without manual intervention | ≥ 95% of trading days |

Also reported, but not gating: per-autonomy-level results (`L0`/`L1`/`L2`), win
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
