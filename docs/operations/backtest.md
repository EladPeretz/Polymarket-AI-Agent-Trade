# Running the playbook backtest (Gate 1)

`perps-agent backtest` screens every playbook version over the rolling window
(`backtest.window_days`, 91 calendar days) on all enabled symbols. It reads
public market data only, needs no credentials, and never touches orders.
Design and fill model: [overview §5](../architecture/overview.md#5-testing-and-evaluation-strategy);
criteria: [go-no-go.md](../architecture/go-no-go.md).

```bash
perps-agent backtest                          # all enabled symbols, default parameters
perps-agent backtest --symbol MSTR-USD        # one symbol (repeatable)
perps-agent backtest --params tuned.yaml      # candidate parameters, by playbook id
perps-agent publish                           # push the records and report to paper-results
```

`tuned.yaml` has the same shape as playbook overrides, for example
`{momentum_scalp: {target_r: 3}}`; values outside a playbook's bounds are
refused. Different parameters make a different version tag, and the record is
written under that tag.

* **First run** fills the cache (`data/backtest/`, git-ignored): 1-second mark
  history for every window day and symbol, about 15 requests per symbol and
  day, so roughly half an hour for six symbols. Later runs fetch only the new
  days and replay in a few minutes.
* **Output:** `results/gate1/<tag>.json` per version (verdict, criteria,
  breakdown by regime and symbol, a sample of trades for the trigger review)
  and `results/backtests/<date>/` (`report.md`, `report.json`, `trades.jsonl`).
* **Effect on paper trading:** a version whose record is a pass is screened
  (may trade as L0/L1) from the next session that has the record. The daily
  session's `fetch-results` brings the records from `paper-results`, so publish
  after running the backtest elsewhere. The owner-exempt initial versions
  (`agent.exempt_playbooks`) stay screened whatever their record says.
* **Hold-out:** the last `backtest.holdout_days` (21) of the window are reported
  apart ("design" and "hold-out" rows). Tune playbooks on the design period
  only and look at the hold-out once, so a change is judged on days it was not
  fitted to. Gate 1 itself uses the whole window.
* **Target reachability:** each trade records the expected move over its
  planned hold (1-minute volatility × √minutes to the time stop) and its
  target's distance in those units (`target_reach`); the report groups the
  design period by it.
* **Re-validation:** rerun weekly as the window rolls (ADR-0008), and for every
  new playbook version before it is used.
