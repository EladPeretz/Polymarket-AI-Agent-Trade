# ADR-0009: Agent-managed positions: adding, exit plans, trailing stops and runners

Status: Proposed · Date: 2026-10-19

## Context

The owner wants the agent to reason about each case, not just pick a playbook:
the trade's shape and size, adding to winners, trailing stops and letting
runners win, all context- and regime-dependent. The initial design allowed only a
fixed stop/target and full exits. Polymarket's native trailing stops are not
enabled, and position TP/SL updates are cancel-then-create (non-atomic).

## Decision

* The agent may attach an **exit plan** to a trade and replace it **at any time**:
  scale-out steps, a breakeven trigger, and a trailing rule. Rule types are a
  vocabulary (volatility, structure, VWAP, fixed distance, breakeven-after-R). The
  agent chooses and parameterizes them per case.
* The **engine executes** trailing rules and exit plans continuously, so trailing
  doesn't depend on how often the agent wakes.
* The agent may **add to a winning position**. The combined risk of all entries to
  the current stop must stay within the position's approved risk budget.
* The agent may take **partial exits**.
* Fixed constraints: stops only tighten (never widened or removed); total
  position risk never exceeds its approved budget; the 12:30 close-out is strict,
  runners included; all §3.7 risk checks apply to every action.
* Every management action is journaled, and reports compare the realized
  management against a simple fixed-exit baseline, per regime.

## Consequences

* The agent has real discretion over trade shape and management, while the
  worst case stays bounded by the original risk budget.
* More engine logic (rule evaluation, stop replacement with a self-watch during
  the non-atomic gap) and more simulator logic, each with tests.
* Whether active management adds value becomes measurable, per regime.
