# Polymarket Perps AI Agent

An LLM-agent trading bot for **stock perpetuals on Polymarket Perps**. The agent
makes trading decisions inside a framework of hard risk limits that it cannot
override.

## Project phases

| Phase | Goal | Status |
| - | - | - |
| 0 — Venue research | Read the venue docs, probe the live API, list blockers and gaps | **Done** — see [`docs/phase-0/`](docs/phase-0/) |
| 1 — Architecture | System design, ADRs, go/no-go criteria for the merge decision | **Done** (`v0.1.0`) — see [`docs/architecture/`](docs/architecture/overview.md), [`docs/adr/`](docs/adr/README.md) |
| 2 — Development | Exchange adapter, risk engine, agent framework, data recorder | **In progress** — work packages in [overview §8](docs/architecture/overview.md) |
| 3 — Simulation testing | Backtests → paper trading on live feeds → small live capital | — |
| Decision | Merge and continue, or fall back to the previous tagged version | — |

## Phase 0 deliverables

* [Venue research](docs/phase-0/venue-research.md): auth model, instruments,
  pricing, funding, fees, margin, liquidation, data availability, SDK assessment.
* [Open questions](docs/phase-0/open-questions.md): blockers and decisions
  needed before architecture sign-off.
* [`scripts/phase0_connectivity_check.py`](scripts/phase0_connectivity_check.py):
  read-only connectivity check (never places orders or creates credentials).

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# checks (same as CI)
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest

# validate configuration (config/default.yaml + config/symbols/*.yaml)
.venv/bin/perps-agent config-check            # warns about unset risk values
.venv/bin/perps-agent config-check --strict   # fails unless ready to run
```

Stream live market data for the enabled symbols and record it to
`data/recordings/` (read-only, no credentials; needs WebSocket access):

```bash
.venv/bin/perps-agent record --seconds 60   # prints the final snapshots
```

Read-only venue connectivity check (never places orders):

```bash
.venv/bin/python scripts/phase0_connectivity_check.py --symbol NVDA-USD
```

## Important constraints

* **Geographic restriction:** Polymarket Perps forbids order placement from the
  US, Canada and several other jurisdictions. Live trading must run only from a
  permitted location. Development and read-only data work are unaffected.
* **Secrets:** never commit private keys or proxy secrets. The bot only holds
  the Perps proxy (trading) key, which can trade but cannot withdraw.
