# ADR-0012: The agent is the trader; playbooks are context

Status: Accepted · Date: 2026-10-22

## Context

The WP13 backtest found no lasting edge in any of the six coded playbooks.
Refined entries designed on Jul 22 – Aug 31 faded when forward-tested on
September and October. The owner's view: fixed directional rules stop working
once they are known; what moves price is participants' sentiment and their
reactions to price, including forced moves (trapped traders, leveraged
positions shaken out, hedging around events). The trader's job is to read
those participants, listen to the market, change sides when proven wrong and
adapt when today's dynamics differ from yesterday's. The owner wants the agent
to trade that way within a fixed framework and to build skill from session to
session through its journal and lessons, judged on whether the net result is
consistently positive.

## Decision

* **Discretion is the primary mode.** Discretionary (L2) trades use the full
  per-trade risk budget (`risk.l2_risk_fraction: 1`) and count only against the
  overall daily entry cap (`risk.max_l2_trades_per_day` equal to
  `max_trades_per_day`). The agent sizes 0–1 of the budget by conviction, as
  for any trade.
* **Playbooks become context.** Setups stay in snapshots as patterns other
  participants watch. Screened versions may still be taken as L0/L1, and Gate 1
  keeps screening them, but the agent is told they carry no edge on their own.
* **The system prompt states the owner's approach:** read participants and
  flows rather than out-guess a price that is already set; cut losers; change
  sides only when the evidence shows the other side is winning, as a new trade
  through the risk engine; adapt when recent lessons do not fit today; name
  each read with a stable `setup` key so the learning loop can tell which reads
  pay.
* **Positioning data in every snapshot:** open interest, the perp premium over
  the underlying index (`premium_bp`) and recent trade flow split by aggressor
  (`flow`), next to funding.
* **The framework does not move:** stop floor, daily loss limit, concurrent
  positions, trading window and guardian are unchanged and identical at every
  level (ADR-0003). Later owner decisions the same day raised the daily entry
  cap to 30 and lowered the minimum reward to risk for a set target to 1.3R, so
  targets can match what the market can reach.
  Live trading still needs its own approval (ADR-0004).

## Consequences

* Paper results now mostly measure the agent's own judgement. With roughly
  40 trades a month, telling skill from luck takes months; results are judged
  by read (`setup` key) and over time, not session by session.
* The learning loop (WP14: daily review, lessons with evidence counts, lessons
  in the briefing) becomes the core of the system rather than an add-on.
* Discretionary losses can be as large as playbook losses. The daily loss
  limit and per-trade budget still cap them.
* Reverting is a config change (`l2_risk_fraction`, `max_l2_trades_per_day`).
