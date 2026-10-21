# Scheduling the daily paper session

How the daily paper session is started (WP12, ADR-0004). The session itself
follows [daily-session.md](daily-session.md).

## The scheduled task

A Claude Code scheduled task (a "routine") starts a fresh cloud session every
weekday at 09:10 New York time. Holidays need no special handling: the session
runs `perps-agent preflight`, which exits 3 on NYSE holidays, and the session
ends there.

| Setting | Value |
| - | - |
| Repository | `EladPeretz/Polymarket-AI-Agent-Trade` (default branch) |
| Schedule | `CRON_TZ=America/New_York 10 9 * * 1-5` (09:10, Monday to Friday; daylight saving handled by the time zone) |
| Each run | a new session (nothing carries over between days except `paper-results`) |
| Environment | the project's cloud environment; its network policy must allow `api.perpetuals.polymarket.com`, `ws.perpetuals.polymarket.com` and GitHub |
| Credentials | none (paper mode reads public market data) |
| Connectors | none |
| Prompt | below |

09:10 leaves twenty minutes for install, preflight, fetching past results and
loading market history before the window opens at 09:30.

### Prompt

The prompt is kept short: the procedure lives in the repository, so changes go
through pull requests rather than edits to the scheduled task. The push
permission has to be in the prompt itself, because a scheduled session only
pushes to `claude/` branches unless its prompt says otherwise.

```text
This is the daily Polymarket Perps paper-trading session (paper mode only: no
real orders, no credentials). Nobody is watching, so do not stop to ask
questions. In this repository, follow docs/operations/daily-session.md from
step 1 to step 5, in this one session, and do not end your turn until step 5
is done or the runbook says to stop.

You are explicitly allowed to push to the `paper-results` branch, and only
through `scripts/paper_session.sh finish` (which runs `perps-agent publish`).
Do not push to any other branch, do not open pull requests or issues, and do
not change code or configuration.
```

## Results branch

`perps-agent publish` pushes `results/<date>/` to `paper-results` (ADR-0006).
The first publish creates the branch. Checked on 2026-10-21:

* No GitHub ruleset or branch protection covers `paper-results`. Adding one later
  that blocks pushes to it would stop publishing; publish then exits 1 and
  records a critical `publish_failed` incident.
* The cloud environment's GitHub proxy does not limit which branches a session
  pushes to; only the prompt (above) and GitHub rules do.

Recommended once the branch exists: a GitHub ruleset on `paper-results` that
**restricts deletions** and **blocks force pushes**, and nothing else (no
required reviews or status checks, which would block the bot's pushes). Settings
→ Rules → Rulesets → New branch ruleset, target `paper-results`.

Fallback if pushing to `paper-results` is refused and cannot be fixed: set
`results.branch: claude/paper-results` in `config/default.yaml` (one-line PR).
Scheduled sessions may always push to `claude/` branches.

## Rehearsals

`perps-agent run --rehearsal MINUTES` runs the full session (live feed, engine,
simulator, guardian, bridge, report) on a window of `MINUTES` (10 to 180)
starting at the next minute, at any time of day. Results go to
`data/rehearsals/<timestamp>/`, never to `results/`, so a rehearsal is never
published and never blocks the day's real session. Overnight books are thin, so
the risk engine's depth and spread checks reject most entries outside regular
hours; that is the risk engine working, not a fault.

```bash
.venv/bin/perps-agent run --rehearsal 15 &
.venv/bin/perps-agent agent get_briefing
.venv/bin/perps-agent agent next_event '{"timeout_seconds": 60}'
```

## Open risks (ADR-0004)

* **Session length.** The session runs about three and a half hours in one turn,
  with a tool call at least every nine minutes. The dry run is the first test of
  that in a scheduled session.
* **Usage.** A scheduled session counts against the account's Claude usage like
  any other session. A full trading morning is a long session with many tool
  calls; watch the usage after the first days.
* **Context.** Each `next_event` answer is about 0.5 to 3 KB; the session relies
  on automatic context compaction over the morning. The system prompt and
  briefing can be re-read at any time (`perps-agent prompt`, `get_briefing`).
