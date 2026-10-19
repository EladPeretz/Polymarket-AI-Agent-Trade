# Polymarket Perps AI Agent

An LLM-agent trading bot for **stock perpetuals on Polymarket Perps**. The agent
makes trading decisions inside a framework of hard risk limits that it cannot
override.

## Project phases

| Phase | Goal | Status |
| - | - | - |
| 0 — Venue research | Read the venue docs, probe the live API, list blockers and gaps | **Done** — see [`docs/phase-0/`](docs/phase-0/) |
| 1 — Architecture | System design, ADRs, go/no-go criteria for the merge decision | Next |
| 2 — Development | Exchange adapter, risk engine, agent framework, data recorder | — |
| 3 — Simulation testing | Backtests → paper trading on live feeds → small live capital | — |
| Decision | Merge and continue, or fall back to the previous tagged version | — |

## Phase 0 deliverables

* [Venue research](docs/phase-0/venue-research.md): auth model, instruments,
  pricing, funding, fees, margin, liquidation, data availability, SDK assessment.
* [Open questions](docs/phase-0/open-questions.md): blockers and decisions
  needed before architecture sign-off.
* [`scripts/phase0_connectivity_check.py`](scripts/phase0_connectivity_check.py):
  read-only connectivity check (never places orders or creates credentials).

## Running the connectivity check

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # optional: fill in for account checks
set -a; . ./.env; set +a
.venv/bin/python scripts/phase0_connectivity_check.py --symbol NVDA-USD
```

## Important constraints

* **Geographic restriction:** Polymarket Perps forbids order placement from the
  US, Canada and several other jurisdictions. Live trading must run only from a
  permitted location. Development and read-only data work are unaffected.
* **Secrets:** never commit private keys or proxy secrets. The bot only holds
  the Perps proxy (trading) key, which can trade but cannot withdraw.
