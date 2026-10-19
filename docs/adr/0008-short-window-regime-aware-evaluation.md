# ADR-0008: Short-window, regime-aware strategy evaluation with frequent iteration

Status: Proposed · Date: 2026-10-19

## Context

* Polymarket stock perps have ~3 months of history.
* The owner's view: strategies must adapt every couple of months. Long backtests
  mostly measure regimes that no longer apply.
* Comparing strategies across unlike conditions is misleading. For example, a
  momentum strategy evaluated on compressed, low-volatility sessions looks broken
  when it is simply out of its element.

## Decision

* **Data window:** backtests use a rolling ~3-month window of Polymarket data. No
  external long-history source for now.
* **Regime tagging:** every session (and every trade, backtest or paper) is tagged
  with regime labels computed by deterministic features (e.g. expansion vs
  compressed, trend vs rotation). Labels are also given to the agent as context.
* **Declared applicability:** each playbook declares the regimes it is built for.
  Evaluation (Gate 1 and paper reports) judges it within those regimes and checks
  that it stays quiet outside them.
* **Versioning:** a playbook change (logic or parameters) creates a new version.
  Results are attributed per version, and each new version passes the Gate 1 screen
  before paper trading.
* **Role of backtests:** a sanity screen (fires on the right setups, no
  in-regime bleed after costs), not statistical proof. Paper trading is the main
  evidence.
* **Re-validation cadence:** the screen re-runs automatically as the window rolls
  (e.g. weekly), so we see when a playbook's target regime disappears or its edge decays.

## Consequences

* Fast strategy iteration is a normal, tracked activity rather than an exception.
* Small samples mean individual results are noisy. Reports show trade counts and
  regime coverage so decisions aren't made on a handful of trades.
* The regime classifier becomes an important component and needs its own tests
  and periodic review.
