# Daily paper session runbook

The procedure the scheduled Claude Code cloud session follows each weekday
(WP12, ADR-0004, overview §4). It is written for that session: nobody is at the
keyboard, so it never stops to ask a question. The owner sets up the schedule
once, as described in [scheduling.md](scheduling.md).

Paper mode only. No credentials are needed and no real order can be placed: the
engine refuses any other mode, and live trading never runs in a cloud session
(`CLAUDE.md`).

## Rules for the session

* **Stay in this one session until step 5 is done.** The session ends when you
  stop working, and the engine and its results die with the container. Never end
  your turn while the engine is running or before the results are published.
* **Every command must finish within 10 minutes.** `next_event` waits at most
  540 seconds and the scripts below stay under the limit; never start a longer
  `sleep` or wait.
* **Git:** the only push allowed is the results branch, `paper-results`, and only
  through `scripts/paper_session.sh finish` (which runs `perps-agent publish`).
  Do not commit to, push or create any other branch, and do not open pull
  requests or issues.
* **Do not change code, configuration, the kill switch or anything under
  `config/`.** If something is broken, record it (step 5) and let the owner fix it
  in a pull request.
* Keep your messages short. The session lasts about three and a half hours, and
  every tool answer stays in your context.

## 1. Start (from about 09:10 New York)

```bash
scripts/paper_session.sh start
```

It installs the package, runs `perps-agent preflight`, copies the last ten days
of results back from `paper-results` (for the briefing), starts the engine in the
background (`perps-agent run`, log in `data/engine.log`) and waits until the
agent's tools answer.

* **Exit 3**: not a trading day, or the window has already ended. Say so in one
  line and end the session. Nothing else to do.
* **Exit 1**: preflight or start-up failed. Its output says which check. Retry
  once after a minute if the cause looks transient (network, rate limit);
  otherwise go to step 5 and report the failure.
* **Exit 0**: the engine is running. Continue.

## 2. Read your instructions

```bash
.venv/bin/perps-agent prompt
```

This prints your system prompt as the trader, and today's briefing. From here
until the window closes you are that trader: follow that prompt. Call the tools
as `.venv/bin/perps-agent agent <tool> '<json>'` (or activate the virtualenv
first with `source .venv/bin/activate`).

## 3. Trade the window (09:30 to 12:30)

Call `next_event` in a loop with `{"timeout_seconds": 540}` and act on what it
returns, as the system prompt describes. Before 09:30 it returns the
`session:open` event when the window opens. Keep calling it: a timeout with no
events is normal. If you make no tool call for 10 minutes during the window the
guardian holds new entries.

If a tool answers "engine not reachable", check the engine:

```bash
scripts/paper_session.sh status
```

If it is not running, do not restart it (`run` refuses to start a second session
on the same day). Look at the end of `data/engine.log`, then go to step 4.

## 4. After the close

When `next_event` returns `session:close`, the guardian closes anything still
open. Keep calling `next_event` until the engine stops answering, then:

```bash
scripts/paper_session.sh finish
```

It waits for the engine to exit (exit 4: still running, run it again), prints
the warnings and errors from the engine log, rebuilds the daily report if
needed and publishes `results/<date>/` to `paper-results` (`perps-agent
publish`, retried on its own). Exit 1 means the publish failed; the failure is
recorded as a `publish_failed` incident and the next session retries it. Run
`finish` once more after a minute before giving up.

## 5. Wrap up

Read `results/<date>/report.md` and end the session with a short summary (at
most ten lines): trades and net PnL, incidents, anything that did not work
(start-up, tools, engine, publish), and what you would want the owner to look at.
The daily review and lessons notebook arrive with WP14; until then this summary
is the review.
