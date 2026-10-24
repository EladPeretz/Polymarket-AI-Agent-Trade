# ADR-0004: Paper trading in Claude Code cloud sessions; live trading on a self-hosted VPS

Status: Accepted · Date: 2026-10-19

## Context

Polymarket Perps prohibits order placement from the US, Canada and other
jurisdictions. Market data is unrestricted. Claude Code cloud sessions cannot be
placed in a chosen region and should be assumed to egress from a restricted one.
The owner prefers Claude Code as the agent runtime.

## Decision

* **Paper:** a scheduled task starts a Claude Code cloud session each NYSE trading
  day around 09:10 New York time. The session runs the engine in `paper` mode and
  acts as the agent. No credentials are needed.
* **Live:** the same repo, engine and Claude Code agent run on a VPS in a
  permitted jurisdiction, started by the server's scheduler. Moving there is a
  prerequisite for `live` mode and happens only after Gate 2.
* Routing traffic to appear to come from a permitted jurisdiction is out of scope
  and will not be built.
* `live` mode requires an explicit flag plus a jurisdiction attestation in config,
  and refuses to start in CI and dev environments.

## Consequences

* Paper trading can start without a server or credentials.
* Open risks, to verify early in Phase 2: cloud session duration over a 3+ hour
  run; tool-call time caps; whether running scheduled autonomous sessions on a
  subscription fits its usage terms and limits (else an API key). The design
  survives an agent outage (ADR-0002).
* Live mode adds VPS operations: deploys, secrets, monitoring.
* WP12 (2026-10-21): the schedule, runbook and start/finish script are in
  `docs/operations/`. The first full dry run of a scheduled session is the first
  test of session duration and tool-call caps; its findings are recorded there.
