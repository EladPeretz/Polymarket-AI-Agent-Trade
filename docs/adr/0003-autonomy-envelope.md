# ADR-0003: Agent autonomy as a config-defined envelope

Status: Proposed · Date: 2026-10-19

## Context

The owner wants a mix of full agent discretion and a predetermined framework,
and expects to work out where the line falls over time. Playbooks can be
backtested; free LLM discretion cannot (the model may know historical outcomes).

## Decision

Autonomy is expressed as levels, enabled and capped per symbol in config:

* `L0` filter: the agent approves or vetoes engine-generated playbook signals.
* `L1` playbook: the agent picks a playbook, direction, size and stop/target
  within playbook limits, or stays out.
* `L2` discretionary: the agent proposes trades not tied to a playbook, with a
  reduced risk budget and a daily count limit.

Every trade is tagged with its level and playbook. Reports attribute results per level.

## Consequences

* Moving the freedom boundary is a config change backed by evidence, not a rewrite.
* Paper results show separately whether playbooks, agent selection or agent
  discretion add value.
* Risk rules are identical at every level. Autonomy changes what may be proposed,
  not what is allowed.
