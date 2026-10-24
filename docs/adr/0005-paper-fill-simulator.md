# ADR-0005: Conservative in-house fill simulator for paper trading

Status: Accepted · Date: 2026-10-19

## Context

Polymarket Perps has no testnet or sandbox; the docs and specs list only
production endpoints. Paper trading therefore needs simulated execution against
live data. Optimistic fill assumptions are the most common reason paper results
fail to carry over to live trading.

## Decision

* Marketable orders walk the live book at decision time plus a latency allowance.
* Resting limits fill only when a public trade prints **through** the price; no
  queue priority is assumed.
* Fees at the account's tier; live hourly funding; stops and targets triggered by
  **mark price** (as on the venue); liquidation modelled as a safety assertion.
* The same simulator backs historical backtests with a reduced data model.

## Consequences

* Paper PnL is biased conservative, which is preferable for a go/no-go decision.
* Gate 3 compares live fills with the simulator's model to validate it.
* The simulator is non-trivial code with its own test suite.
