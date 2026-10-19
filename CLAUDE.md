# CLAUDE.md

Guidance for Claude Code sessions working in this repository: development
sessions, and the daily paper-trading sessions.

## Project

LLM-agent trading bot for Polymarket Perps stock perpetuals. Start with
`README.md`, then `docs/architecture/overview.md`, `docs/adr/` and
`docs/phase-0/venue-research.md`.

## Safety rules (non-negotiable)

* **Never place, modify or cancel real orders** from a development or cloud
  session. Live mode runs only on the approved VPS, behind its config gate.
* **Never handle the owner (wallet) private key.** Deposits, withdrawals and
  proxy-key creation are done by the owner. If anything that looks like a private
  key appears, do not use, store or echo it; warn the owner.
* Never commit secrets. Credentials come from environment variables only.
* Risk engine and guardian rules are not to be weakened to make a trade or test pass.
* Never route traffic to evade Polymarket's geographic restrictions.

## Conventions

* Python 3.12+, asyncio, `Decimal` for all prices/quantities/money, timezone-aware UTC datetimes.
* Only `perps_agent.venue` may import the `polymarket` SDK (ADR-0001).
* The SDK is pinned to an exact version in `pyproject.toml`; upgrade it only in a dedicated PR.
* Never hard-code venue parameters. Read instruments, fees and limits from the API.
* Lint/format with `ruff`; test with `pytest`. Run `ruff check . && ruff format --check . && pytest`
  before committing.
* Changes go through PRs; the owner reviews and merges. Keep PRs to one work package.

## Known venue/SDK issues

* `GET /v1/info/tickers` ignores `instrument_id`; the SDK's `fetch_perps_ticker`
  returns the wrong instrument. Filter tickers by `instrument_id` yourself.
* Market session (regular/overnight/weekend) is not exposed by the API. Use the
  local calendar.
