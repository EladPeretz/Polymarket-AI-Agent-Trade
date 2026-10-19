# ADR-0001: Python, with the official SDK pinned behind our own adapter

Status: Proposed · Date: 2026-10-19

## Context

Polymarket publishes an official Python SDK (`polymarket-client`) that implements
Perps signing, sessions, streams and pagination. Its Perps APIs are marked
"experimental, may change in a breaking way in any release". Phase 0 also found
a real bug: single-ticker lookup returns the wrong instrument. The venue API
itself changes weekly.

## Decision

* Python 3.12+, asyncio throughout.
* Use `polymarket-client`, **pinned to an exact version**. Upgrades are deliberate PRs
  that rerun contract tests.
* All SDK use is confined to `perps_agent.venue`, which exposes our own interfaces
  and domain models. No other package imports the SDK.
* Known SDK/API issues are worked around in the adapter, with a regression test each.

## Consequences

* SDK churn and bugs are contained in one package and caught by contract tests.
* We can replace the SDK with direct REST/WebSocket calls later (signing is
  documented) without touching strategy, risk or agent code.
* A small amount of model-mapping code to maintain.
