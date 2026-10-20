# ADR-0007: Engineering workflow

Status: Accepted · Date: 2026-10-19

## Context

The project is meant to be maintained to organizational standards, with explicit
merge-or-roll-back decisions at the end of each phase.

## Decision

* `main` is protected. All changes arrive by pull request; **the owner reviews and merges**.
* One PR per work package, with a description of what changed and how it was verified.
* CI on every PR: `ruff` lint and format check, type check, `pytest`. A PR must be
  green before review.
* A version tag on `main` at the end of each phase (`v0.1.0` = Phase 1). Rollback =
  redeploy the last tag that passed its gate.
* Secrets never in the repo. `.env` is git-ignored, and CI scans for leaked keys.
* `CLAUDE.md` at the repo root records conventions and safety rules for the
  Claude Code sessions that develop and run the system.

## Consequences

* Every change is reviewed and reproducible. Phase decisions map to tags.
* Slightly slower iteration, accepted in exchange for safety on trading code.
