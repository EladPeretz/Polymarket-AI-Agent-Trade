# ADR-0006: Paper results persisted to a `paper-results` git branch

Status: Accepted · Date: 2026-10-19

## Context

Cloud sessions are temporary. Each day's events, decisions and reports must
survive the session and be reviewable.

## Decision

At the end of each session, `results/<YYYY-MM-DD>/` (event log, decision journal,
daily report) is committed to a dedicated `paper-results` branch. It is never
merged into `main`. Multi-day summaries are generated from that branch.

## Consequences

* Results are versioned, diffable and visible on GitHub with no extra service.
* Session-end publishing must be robust. If it fails, the guardian records the
  incident, and the next session retries publishing the previous day's results.
* Repository size grows slowly. Raw recordings of books and trades are larger and
  may move to object storage later (separate ADR).
