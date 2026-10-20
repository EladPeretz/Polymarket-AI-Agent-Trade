# ADR-0002: The LLM proposes, deterministic code disposes

Status: Accepted · Date: 2026-10-19

## Context

The agent is an LLM. LLM output is non-deterministic, can be wrong, and can be
influenced by its inputs. Leveraged perps punish a single bad action: at 10x, a
5% adverse move reaches maintenance margin.

## Decision

* Claude interacts only through a narrow tool surface (`propose_trade`,
  `adjust_protection`, `close_position`, read-only state tools). It never signs,
  sends orders or touches credentials.
* Every proposal passes a deterministic risk engine that approves, resizes
  **down** or rejects it, with reasons returned to the agent.
* The agent sizes in risk units (fraction of the per-trade risk budget). The risk
  engine derives quantity from the stop distance.
* Protective stops, the 12:30 close-out, the kill switch and liveness checks are
  enforced by code that does not depend on the LLM.

## Consequences

* The worst case of an agent error is bounded by risk config, not by the agent.
* An agent outage (rate limit, session loss) degrades to "no new entries,
  existing positions protected", not to unmanaged exposure.
* Some agent ideas will be rejected by rules. Rejections are journaled and reviewed.
