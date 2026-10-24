# CLAUDE.md

Guidance for Claude Code sessions working in this repository: development
sessions, and the daily paper-trading sessions.

## Project

LLM-agent trading bot for Polymarket Perps stock perpetuals. Start with
`README.md`, then `docs/architecture/overview.md`, `docs/adr/` and
`docs/phase-0/venue-research.md`.

## Daily paper-trading sessions

A scheduled session follows `docs/operations/daily-session.md` from start to
end without asking questions. Its only allowed push is `paper-results`, through
`scripts/paper_session.sh finish`. Setup and the scheduled task's prompt are in
`docs/operations/scheduling.md`.

## Safety rules (non-negotiable)

* **Never place, modify or cancel real orders** from a development or cloud
  session. Live mode runs only on the approved VPS, behind its config gate.
* **Never handle the owner (wallet) private key.** Deposits, withdrawals and
  proxy-key creation are done by the owner. If anything that looks like a private
  key appears, do not use, store or echo it; warn the owner.
* Never commit secrets. Credentials come from environment variables only.
* Risk engine and guardian rules are not to be weakened to make a trade or test pass.
* Never route traffic to evade Polymarket's geographic restrictions.
* The trading agent's learned state (lessons, playbook parameters) may change only
  within the bounds defined in `main`. It must never alter risk rules, the trading
  window, the guardian, the autonomy envelope or code (ADR-0010).

## Conventions

* Python 3.12+, asyncio, `Decimal` for all prices/quantities/money, timezone-aware UTC datetimes.
* Only `perps_agent.venue` may import the `polymarket` SDK (ADR-0001).
* The SDK is pinned to an exact version in `pyproject.toml`; upgrade it only in a dedicated PR.
* Never hard-code venue parameters. Read instruments, fees and limits from the API.
* Before committing run: `ruff check . && ruff format --check . && mypy && pytest`
  (CI runs the same, plus `perps-agent config-check`).
* Config lives in `config/` (YAML, no secrets). Session times must be quoted `"HH:MM"`
  strings. Capital-dependent risk values stay `null` until the owner sets them; the
  engine refuses to start while any are `null`.
* Changes go through PRs; the owner reviews and merges. Keep PRs to one work package.

## Known venue/SDK issues

* `GET /v1/info/tickers` ignores `instrument_id`; the SDK's `fetch_perps_ticker`
  returns the wrong instrument. Filter tickers by `instrument_id` yourself.
* Market session (regular/overnight/weekend) is not exposed by the API. Use the
  local calendar.
* After an SDK upgrade or a venue API change, re-record the contract fixtures with
  `scripts/record_venue_fixtures.py` and run `pytest tests/contract`. Live read-only
  smoke tests: `PERPS_AGENT_LIVE_TESTS=1 pytest tests/live`. Streaming (WebSockets)
  from a cloud session needs the environment's Full network access.
* WebSocket `sq` is an engine-wide sequence, not a per-channel counter: it increases
  by irregular steps, so never treat a jump as a gap.
