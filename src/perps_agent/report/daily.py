"""The daily report (overview §3.12): one session's results, built from its files.

:func:`build_report` reads ``results/<date>/`` (ledger, journal, incidents,
equity samples, agent latency) and returns a :class:`DailyReport`: incidents,
PnL (gross, fees, funding, net), drawdown, every trade, attribution per
autonomy level, source and symbol, risk verdicts and rejections, decisions and
agent latency. Everything comes from the files, so a report can be rebuilt at
any time (``perps-agent report``).

Scenarios, probabilities and assumptions are for the learning loop only and
are never shown (ADR-0010).
"""

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from perps_agent.agent.latency import AGENT_EVENTS_FILE, EventLatency, load_latencies
from perps_agent.clock import utc_now
from perps_agent.guardian import INCIDENTS_FILE, Incident, Severity, load_incidents
from perps_agent.journal import (
    JOURNAL_FILE,
    ActionFailed,
    DecisionMade,
    JournalEntry,
    RiskVerdictRecorded,
    SessionStarted,
    load_journal,
    trade_outcome,
)
from perps_agent.ledger import LEDGER_FILE, DayStarted, Ledger, LedgerPosition, load_events
from perps_agent.report.equity import EQUITY_FILE, EquitySample, load_equity, max_drawdown
from perps_agent.risk import Decision, Direction

REPORT_JSON = "report.json"
REPORT_MD = "report.md"

_ZERO = Decimal(0)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TradeLine(_Model):
    symbol: str
    direction: Direction
    level: str
    source: str
    opened_at: datetime
    closed_at: datetime
    holding_minutes: Decimal
    max_quantity: Decimal
    average_entry: Decimal
    average_exit: Decimal
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    approved_risk: Decimal
    r_multiple: Decimal | None
    exit_reason: str | None


class OpenPosition(_Model):
    symbol: str
    direction: Direction
    quantity: Decimal
    average_entry: Decimal


class Attribution(_Model):
    key: str
    trades: int
    wins: int
    net_pnl: Decimal
    total_r: Decimal
    average_r: Decimal | None


class VerdictLine(_Model):
    at: datetime
    symbol: str
    level: str
    decision: Decision
    checks: tuple[str, ...]
    detail: str


class FailureLine(_Model):
    at: datetime
    symbol: str | None
    error: str


class LatencyStats(_Model):
    events: int
    answered: int
    pickup_median_seconds: float | None
    pickup_max_seconds: float | None
    answer_median_seconds: float | None
    answer_p90_seconds: float | None
    answer_max_seconds: float | None


class DailyReport(_Model):
    day: date
    mode: str | None
    generated_at: datetime
    symbols: tuple[str, ...]
    incidents: tuple[Incident, ...]
    start_equity: Decimal | None
    end_equity: Decimal | None
    return_fraction: Decimal | None
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    max_drawdown: Decimal
    max_drawdown_fraction: Decimal | None
    trades: tuple[TradeLine, ...]
    open_at_end: tuple[OpenPosition, ...]
    by_level: tuple[Attribution, ...]
    by_source: tuple[Attribution, ...]
    by_symbol: tuple[Attribution, ...]
    decisions: dict[str, int]
    verdicts: dict[str, int]
    rejections: tuple[VerdictLine, ...]
    resizes: tuple[VerdictLine, ...]
    failures: tuple[FailureLine, ...]
    latency: LatencyStats

    @property
    def critical_incidents(self) -> tuple[Incident, ...]:
        return tuple(i for i in self.incidents if i.severity is Severity.CRITICAL)


def build_report(
    day_dir: Path, *, day: date | None = None, clock: Callable[[], datetime] = utc_now
) -> DailyReport:
    """The report for one ``results/<date>/`` directory; missing files count as empty."""
    events = _read(day_dir / LEDGER_FILE, load_events)
    journal: list[JournalEntry] = _read(day_dir / JOURNAL_FILE, load_journal)
    incidents = _read(day_dir / INCIDENTS_FILE, load_incidents)
    samples = _read(day_dir / EQUITY_FILE, load_equity)
    latencies = _read(day_dir / AGENT_EVENTS_FILE, load_latencies)

    started = next((e for e in events if isinstance(e, DayStarted)), None)
    session = next((e for e in journal if isinstance(e, SessionStarted)), None)
    if day is None:
        day = (
            started.day if started else session.day if session else date.fromisoformat(day_dir.name)
        )
    start = started.equity if started is not None else None
    ledger = Ledger.from_events(start if start is not None else _ZERO, events)
    now = clock()

    closed = [p for p in ledger.positions if not p.is_open]
    trades = tuple(_trade(p, ledger, now) for p in sorted(closed, key=lambda p: p.opened_at))
    positions = ledger.positions
    gross = sum((p.realized_pnl for p in positions), _ZERO)
    fees = sum((p.fees for p in positions), _ZERO)
    funding = sum((p.funding for p in positions), _ZERO)
    net = gross - fees + funding

    end: Decimal | None = None
    if start is not None:
        end = samples[-1].equity if samples and ledger.open_positions else start + net
    drawdown = max_drawdown(start, _with_end(samples, end, now)) if start is not None else _ZERO

    return DailyReport(
        day=day,
        mode=str(session.mode) if session is not None else None,
        generated_at=now,
        symbols=session.symbols if session is not None else (),
        incidents=tuple(incidents),
        start_equity=start,
        end_equity=end,
        return_fraction=(end - start) / start if start and end is not None else None,
        gross_pnl=gross,
        fees=fees,
        funding=funding,
        net_pnl=net,
        max_drawdown=drawdown,
        max_drawdown_fraction=drawdown / start if start else None,
        trades=trades,
        open_at_end=tuple(
            OpenPosition(
                symbol=p.symbol,
                direction=p.direction,
                quantity=p.quantity,
                average_entry=p.average_entry,
            )
            for p in ledger.open_positions
        ),
        by_level=_attribute(trades, lambda t: t.level),
        by_source=_attribute(trades, lambda t: t.source),
        by_symbol=_attribute(trades, lambda t: t.symbol),
        decisions=dict(Counter(str(e.action) for e in journal if isinstance(e, DecisionMade))),
        verdicts=dict(
            Counter(str(e.verdict.decision) for e in journal if isinstance(e, RiskVerdictRecorded))
        ),
        rejections=_verdicts(journal, Decision.REJECTED),
        resizes=_verdicts(journal, Decision.RESIZED),
        failures=_failures(journal),
        latency=_latency(latencies),
    )


def _read[T](path: Path, load: Callable[[Path], list[T]]) -> list[T]:
    return load(path) if path.exists() else []


def _with_end(
    samples: list[EquitySample], end: Decimal | None, now: datetime
) -> list[EquitySample]:
    if end is None:
        return samples
    return [*samples, EquitySample(at=now, equity=end, open_positions=0)]


def _trade(position: LedgerPosition, ledger: Ledger, now: datetime) -> TradeLine:
    outcome = trade_outcome(now, "", position, ledger)
    return TradeLine(
        symbol=position.symbol,
        direction=position.direction,
        level=str(position.level) if position.level is not None else "none",
        source=_source(position.playbook),
        opened_at=outcome.opened_at,
        closed_at=outcome.closed_at,
        holding_minutes=(Decimal(str(outcome.holding_seconds)) / 60).quantize(Decimal("0.1")),
        max_quantity=outcome.max_quantity,
        average_entry=outcome.average_entry,
        average_exit=outcome.average_exit,
        gross_pnl=outcome.realized_pnl,
        fees=outcome.fees,
        funding=outcome.funding,
        net_pnl=outcome.net_pnl,
        approved_risk=outcome.approved_risk,
        r_multiple=outcome.r_multiple,
        exit_reason=outcome.exit_reason,
    )


def _source(playbook: str | None) -> str:
    """A playbook's id without its version, or ``discretionary``."""
    if playbook is None:
        return "discretionary"
    return playbook.split("@", 1)[0]


def _attribute(
    trades: Sequence[TradeLine], key: Callable[[TradeLine], str]
) -> tuple[Attribution, ...]:
    groups: dict[str, list[TradeLine]] = defaultdict(list)
    for trade in trades:
        groups[key(trade)].append(trade)
    rows = []
    for name in sorted(groups):
        group = groups[name]
        rs = [t.r_multiple for t in group if t.r_multiple is not None]
        total_r = sum(rs, _ZERO)
        rows.append(
            Attribution(
                key=name,
                trades=len(group),
                wins=sum(1 for t in group if t.net_pnl > 0),
                net_pnl=sum((t.net_pnl for t in group), _ZERO),
                total_r=total_r,
                average_r=total_r / len(rs) if rs else None,
            )
        )
    return tuple(rows)


def _verdicts(journal: Iterable[JournalEntry], decision: Decision) -> tuple[VerdictLine, ...]:
    lines = []
    for entry in journal:
        if not isinstance(entry, RiskVerdictRecorded) or entry.verdict.decision is not decision:
            continue
        verdict = entry.verdict
        lines.append(
            VerdictLine(
                at=verdict.evaluated_at,
                symbol=verdict.intent.symbol,
                level=str(verdict.intent.level),
                decision=verdict.decision,
                checks=tuple(dict.fromkeys(str(r.check) for r in verdict.reasons)),
                detail="; ".join(r.detail for r in verdict.reasons),
            )
        )
    return tuple(lines)


def _failures(journal: Sequence[JournalEntry]) -> tuple[FailureLine, ...]:
    symbols = {e.decision_id: e.symbol for e in journal if isinstance(e, DecisionMade)}
    return tuple(
        FailureLine(at=e.at, symbol=symbols.get(e.decision_id), error=e.error)
        for e in journal
        if isinstance(e, ActionFailed)
    )


def _latency(records: Sequence[EventLatency]) -> LatencyStats:
    pickups = [r.pickup_seconds for r in records]
    answers = [s for r in records if (s := r.answer_seconds) is not None]
    return LatencyStats(
        events=len(records),
        answered=len(answers),
        pickup_median_seconds=statistics.median(pickups) if pickups else None,
        pickup_max_seconds=max(pickups) if pickups else None,
        answer_median_seconds=statistics.median(answers) if answers else None,
        answer_p90_seconds=_percentile(answers, 0.9),
        answer_max_seconds=max(answers) if answers else None,
    )


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]
