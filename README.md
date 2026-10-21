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

Print the trading window for a date from the local NYSE calendar (exits 3 on
non-trading days):

```bash
.venv/bin/perps-agent calendar --date 2026-11-27   # early close: window capped at 13:00
```

Run today's paper session (engine, simulator and the agent's tool server), and
call the agent's tools from another shell (ADR-0011):

```bash
.venv/bin/perps-agent run                     # paper mode only; exits 3 on non-trading days
.venv/bin/perps-agent prompt                  # the agent's system prompt and today's briefing
.venv/bin/perps-agent agent tools             # tools and their input schemas
.venv/bin/perps-agent agent next_event '{"timeout_seconds": 60}'
.venv/bin/perps-agent agent get_snapshot '{"symbol": "NVDA-USD"}'
```

After the session: the daily report, and the results branch (ADR-0006). The
guardian's kill switch stops a running session:

```bash
.venv/bin/perps-agent report --date 2026-10-20   # rebuild results/<date>/report.md and .json
.venv/bin/perps-agent publish                    # commit every local day to paper-results and push
.venv/bin/perps-agent fetch-results --days 10    # copy recent days back from paper-results
.venv/bin/perps-agent kill --reason "..."        # close everything, no new entries this session
.venv/bin/perps-agent kill --clear               # remove the switch before the next session
```

The daily scheduled session (WP12) follows
[`docs/operations/daily-session.md`](docs/operations/daily-session.md); setup is in
[`docs/operations/scheduling.md`](docs/operations/scheduling.md):

```bash
.venv/bin/perps-agent preflight               # go / no-go before the session (exit 3: nothing today)
scripts/paper_session.sh start                # install, preflight, fetch results, start the engine
scripts/paper_session.sh finish               # wait for the engine to end, publish the results
.venv/bin/perps-agent run --rehearsal 15      # the full session on a 15-minute window starting now
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
