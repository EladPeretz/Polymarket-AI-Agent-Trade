"""The agent's record over recent sessions: results by read, and calibration (ADR-0010).

* **By read:** closed trades grouped by the decision's ``setup`` key (the
  agent's name for its read, ADR-0012), with net R and win rate, so the
  briefing shows which reads have paid and which only felt convincing.
* **Calibration:** for reviewed entries, the probability the agent gave its
  chosen scenario against how often that scenario is the one that played out.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from perps_agent.journal import Action, Journal, journal_path, load_journal

UNTAGGED = "untagged"
# Upper bounds (exclusive) of the calibration buckets; the last one is open.
CALIBRATION_BUCKETS = ((Decimal("0.4"), "under 40%"), (Decimal("0.6"), "40–60%"))
TOP_BUCKET = "60% and over"


@dataclass(slots=True)
class ReadStats:
    setup: str
    trades: int = 0
    wins: int = 0
    net_r: Decimal = Decimal(0)
    sessions: set[date] = field(default_factory=set)

    @property
    def average_r(self) -> Decimal:
        return self.net_r / self.trades if self.trades else Decimal(0)


@dataclass(slots=True)
class CalibrationBucket:
    label: str
    decisions: int = 0
    probability: Decimal = Decimal(0)
    hits: int = 0

    @property
    def stated(self) -> Decimal:
        return self.probability / self.decisions if self.decisions else Decimal(0)

    @property
    def realized(self) -> Decimal:
        return Decimal(self.hits) / self.decisions if self.decisions else Decimal(0)


@dataclass(frozen=True, slots=True)
class Record:
    sessions: tuple[date, ...]
    reads: tuple[ReadStats, ...]
    calibration: tuple[CalibrationBucket, ...]
    reviewed: int
    unreviewed: int


def _bucket(probability: Decimal) -> str:
    for bound, label in CALIBRATION_BUCKETS:
        if probability < bound:
            return label
    return TOP_BUCKET


def build_record(results_dir: Path, days: Sequence[date]) -> Record:
    """The record over the sessions in ``days`` that have a journal."""
    reads: dict[str, ReadStats] = {}
    buckets = {label: CalibrationBucket(label) for _, label in CALIBRATION_BUCKETS}
    buckets[TOP_BUCKET] = CalibrationBucket(TOP_BUCKET)
    sessions = []
    reviewed = unreviewed = 0
    for day in sorted(days):
        path = journal_path(results_dir, day)
        if not path.exists():
            continue
        sessions.append(day)
        for trail in Journal.from_entries(load_journal(path)).decisions:
            decision = trail.decision
            if decision.action is not Action.ENTER or not trail.orders:
                continue
            key = decision.setup or UNTAGGED
            stats = reads.setdefault(key, ReadStats(key))
            for outcome in trail.outcomes:
                if outcome.r_multiple is None:
                    continue
                stats.trades += 1
                stats.net_r += outcome.r_multiple
                stats.wins += outcome.net_pnl > 0
                stats.sessions.add(day)
            review = trail.review
            if review is None:
                unreviewed += 1
                continue
            reviewed += 1
            chosen = next(s for s in decision.scenarios if s.name == decision.chosen_scenario)
            bucket = buckets[_bucket(chosen.probability)]
            bucket.decisions += 1
            bucket.probability += chosen.probability
            bucket.hits += review.realized_scenario == decision.chosen_scenario
    ordered = sorted(
        (s for s in reads.values() if s.trades), key=lambda s: (s.net_r, s.trades), reverse=True
    )
    return Record(
        sessions=tuple(sessions),
        reads=tuple(ordered),
        calibration=tuple(b for b in buckets.values() if b.decisions),
        reviewed=reviewed,
        unreviewed=unreviewed,
    )
