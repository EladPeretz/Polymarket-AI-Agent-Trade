#!/usr/bin/env bash
# The daily paper session's start and finish steps (WP12, docs/operations/daily-session.md).
#
#   scripts/paper_session.sh start    install, preflight, fetch past results, start the engine
#   scripts/paper_session.sh status   is the engine running? (exit 0 yes, 1 no)
#   scripts/paper_session.sh finish   wait for the engine to end, then publish the results
#
# Each step stays under the cloud session's 10-minute limit for one command.
# Exit codes: 0 done, 1 failed, 3 nothing to do today (non-trading day or the
# window has ended), 4 the engine is still running (call finish again).
set -uo pipefail

cd "$(dirname "$0")/.."
VENV=.venv
BIN="$VENV/bin/perps-agent"
LOG=data/engine.log
PID_FILE=data/engine.pid
# How long `start` waits for the engine's tools, and `finish` for the engine to end.
START_WAIT_SECONDS=${START_WAIT_SECONDS:-480}
FINISH_WAIT_SECONDS=${FINISH_WAIT_SECONDS:-540}

say() { printf '[paper-session] %s\n' "$*"; }

engine_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

install() {
  if [[ ! -x "$BIN" ]]; then
    say "installing into $VENV"
    python3 -m venv "$VENV" || return 1
  fi
  "$VENV/bin/pip" install --quiet --disable-pip-version-check -e . || return 1
}

start() {
  if engine_running; then
    say "engine already running (pid $(cat "$PID_FILE"))"
    return 0
  fi
  install || { say "install failed"; return 1; }

  "$BIN" preflight
  local preflight=$?
  if [[ $preflight -eq 3 ]]; then
    say "nothing to do today"
    return 3
  elif [[ $preflight -ne 0 ]]; then
    say "preflight failed"
    return 1
  fi

  # Past days for the briefing's recent activity. Not fatal: the first day has none.
  "$BIN" fetch-results --days 10 || say "warning: could not fetch past results"

  mkdir -p data
  nohup "$BIN" run "$@" >"$LOG" 2>&1 &
  echo $! >"$PID_FILE"
  say "engine started (pid $!, log $LOG)"

  local waited=0
  while ((waited < START_WAIT_SECONDS)); do
    if ! engine_running; then
      say "engine exited during start-up; last log lines:"
      tail -n 20 "$LOG"
      return 1
    fi
    if "$BIN" agent get_portfolio >/dev/null 2>&1; then
      say "engine ready: the agent's tools answer"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  say "engine not ready after ${START_WAIT_SECONDS}s; last log lines:"
  tail -n 20 "$LOG"
  return 1
}

status() {
  if engine_running; then
    say "engine running (pid $(cat "$PID_FILE"))"
    return 0
  fi
  say "engine not running"
  return 1
}

finish() {
  local waited=0
  while engine_running && ((waited < FINISH_WAIT_SECONDS)); do
    sleep 10
    waited=$((waited + 10))
  done
  if engine_running; then
    say "engine still running; call finish again"
    return 4
  fi
  if [[ -f "$LOG" ]]; then
    say "engine log, warnings and errors:"
    grep -E '"level": "(WARNING|ERROR|CRITICAL)"' "$LOG" | tail -n 20 || true
  fi
  # The engine writes the report on exit; rebuild it in case that failed.
  "$BIN" report >/dev/null || say "warning: no report for today"
  "$BIN" publish
  local published=$?
  if [[ $published -ne 0 ]]; then
    say "publish failed: results are only in results/ in this session"
    return 1
  fi
  say "results published"
  return 0
}

case "${1:-}" in
  start) shift; start "$@" ;;
  status) status ;;
  finish) finish ;;
  *)
    echo "usage: $0 start [run options] | status | finish" >&2
    exit 2
    ;;
esac
