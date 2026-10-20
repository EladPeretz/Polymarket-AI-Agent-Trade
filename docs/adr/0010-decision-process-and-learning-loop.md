# ADR-0010: Scenario-based decisions and an agent-owned learning loop

Status: Accepted · Date: 2026-10-20

## Context

The owner wants the agent to simulate scenarios and likely outcomes before
acting, choosing the most probable trade idea for the context and regime. The
agent's decision-making should improve day to day from logged experience. The
owner decided that (1) scenario reasoning is stored in the journal so the agent
can learn from it, and (2) the agent itself applies what it learns, including
playbook changes.

## Decision

* **Scenario reasoning** is part of how the agent decides, not a separate system
  stage. A `simulate` tool (engine-generated, regime-calibrated price paths)
  gives quantitative outcome distributions on request.
* **Journaling:** scenarios, probabilities, chosen plan and expected outcome are
  recorded for every decision. They feed the learning loop only and are not
  surfaced in reports or alerts.
* **Learning loop:**
  * daily review: calibration, thesis accuracy, management vs a fixed-exit baseline;
  * lessons notebook tagged by regime and setup, with evidence counts;
  * the morning briefing loads relevant lessons and calibration stats;
  * weekly review: prune unsupported lessons and tune playbooks.
* **Agent-owned changes:** the agent updates lessons and playbook
  parameters/variants itself, stored as versioned agent state on the results
  branch.
* **Guardrails on self-modification:**
  * Bounds for every tunable parameter are defined in `main` (owner-reviewed).
    The agent cannot exceed them.
  * Each change is a new playbook version. It must pass the automated Gate 1
    screen before it trades, and is attributed separately in results.
  * A lesson needs repeated evidence before it is used in decisions.
  * Changes appear in the daily report and can be reverted to any prior state.
  * Not learnable: risk rules and values, trading window and cutoffs, guardian,
    autonomy envelope, and code. These change only through owner-reviewed PRs.

## Consequences

* The agent adapts within days, matching the owner's view that strategies must
  evolve continuously.
* Small samples make self-tuning prone to chasing noise. Evidence thresholds,
  versioning, the Gate 1 screen and per-version attribution limit this, and the
  owner can see every change.
* Gate 2's 20-day count is not reset by agent playbook changes (consistent with
  the go/no-go criteria). Safety criteria still apply throughout.
