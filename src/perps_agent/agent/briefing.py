"""The agent's system prompt and session briefing (overview §3.13, §4).

The system prompt (``prompts/system.md``) is fixed: the role, the decision
process and the owner's trading style. The briefing is built each morning
from the config and the results store: the day's window, the symbols and
their notes, the risk limits, the playbooks' Gate 1 status, how many recent
sessions had no trade (owner rule: at most about one a week), the lessons
notebook and the agent's record by read and calibration (ADR-0010).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from perps_agent.calendar import NyseCalendar, TradingWindow
from perps_agent.config.models import AppConfig
from perps_agent.journal import Action, Journal, journal_path, load_journal
from perps_agent.learning import Lesson, LessonBook, LessonStatus, Record
from perps_agent.strategy.history import prior_sessions
from perps_agent.strategy.playbooks import Playbook

# Prior sessions the briefing counts trades over.
RECENT_SESSIONS = 5


def system_prompt() -> str:
    return resources.files("perps_agent.agent").joinpath("prompts/system.md").read_text()


@dataclass(frozen=True, slots=True)
class SessionActivity:
    day: date
    # ``None``: no journal for that day (no session ran).
    entries: int | None


def recent_activity(
    results_dir: Path, exchange: NyseCalendar, today: date, sessions: int = RECENT_SESSIONS
) -> list[SessionActivity]:
    """Entries sent (orders linked to entry decisions) in each prior session, oldest first."""
    activity = []
    for day in prior_sessions(exchange, today, sessions):
        path = journal_path(results_dir, day)
        if not path.exists():
            activity.append(SessionActivity(day, None))
            continue
        journal = Journal.from_entries(load_journal(path))
        entries = sum(
            1 for t in journal.decisions if t.decision.action is Action.ENTER and t.orders
        )
        activity.append(SessionActivity(day, entries))
    return activity


def build_briefing(
    config: AppConfig,
    *,
    day: date,
    window: TradingWindow | None,
    playbooks: Sequence[Playbook[Any]],
    screened: Iterable[str],
    activity: Sequence[SessionActivity],
    lessons: LessonBook | None = None,
    record: Record | None = None,
) -> str:
    zone = config.session.zone
    screened = frozenset(screened)
    risk = config.risk
    lines = [f"# Briefing for {day:%A %Y-%m-%d}", ""]
    if window is None:
        lines += ["Not a trading day: no window today.", ""]
    else:
        warmup = config.features.regime_min_minutes
        regime_from = window.market.open + timedelta(minutes=warmup)
        lines += [
            "## Window (New York)",
            "",
            f"* Trading window {window.start.astimezone(zone):%H:%M}–"
            f"{window.end.astimezone(zone):%H:%M}; entries until "
            f"{window.entry_cutoff.astimezone(zone):%H:%M}; everything is flat by "
            f"{window.end.astimezone(zone):%H:%M}.",
            f"* Regime labels appear from {regime_from.astimezone(zone):%H:%M} "
            f"({warmup} minutes after the open); until then `regime` is null and the "
            "opening range is still forming.",
        ]
        if window.market.early_close:
            lines.append(
                f"* Early close today: the market closes at "
                f"{window.market.close.astimezone(zone):%H:%M}."
            )
        lines.append("")

    lines += ["## Symbols", ""]
    for symbol in config.symbols:
        if not symbol.enabled:
            continue
        levels = [lvl for lvl in ("L0", "L1", "L2") if getattr(symbol.autonomy, lvl)]
        note = f" {symbol.notes}" if symbol.notes else ""
        lines.append(f"* **{symbol.symbol}** ({', '.join(levels)}).{note}")
    lines.append("")

    lines += [
        "## Your limits (enforced by the risk engine)",
        "",
        f"* Full size risks {_pct(risk.per_trade_risk_fraction)} of equity per trade"
        + (
            ", whatever the source."
            if risk.l2_risk_fraction == 1
            else f" (L0/L1); discretionary (L2) trades use {_pct(risk.l2_risk_fraction)} of that."
        ),
        f"* Daily loss limit {_pct(risk.daily_loss_limit_fraction)} of start-of-day equity, "
        "counting what open positions would lose at their stops. It is the only stop for "
        "the day.",
        f"* At most {risk.max_concurrent_positions} positions at once, one per symbol; "
        f"{risk.max_trades_per_day} entries a day"
        + (
            f", of which {risk.max_l2_trades_per_day} L2"
            if risk.max_l2_trades_per_day is not None
            and risk.max_trades_per_day is not None
            and risk.max_l2_trades_per_day < risk.max_trades_per_day
            else ""
        )
        + ". An entry that is cancelled without any fill gives its slot back.",
        f"* Stops at least the 15-minute range from entry (never under "
        f"{risk.min_stop_distance_bp} bp); a target, if set, at least {risk.min_reward_risk}R "
        "after costs.",
        "",
    ]

    lines += [
        "## Playbooks",
        "",
        "Playbook setups are other traders' textbook patterns: context about what many "
        "participants are watching, not instructions. You are the trader.",
        "",
    ]
    passed = [p for p in playbooks if p.tag in screened]
    if passed:
        lines.append("Screened (may trade as L0/L1): " + ", ".join(p.tag for p in passed) + ".")
    exempt = set(config.agent.exempt_playbooks)
    if any(p.tag in exempt for p in passed):
        lines.append(
            "The initial versions are exempt from Gate 1 by owner decision; a changed "
            "version needs the screen."
        )
    pending = [p.id for p in playbooks if p.tag not in screened]
    if pending:
        context = (
            "Their setups still show up, marked unscreened; act on one as a discretionary "
            "trade if your own read agrees."
            if config.agent.unscreened_playbook_signals
            else "Their setups are hidden until they pass."
        )
        lines.append(
            "Not yet through the Gate 1 backtest screen: " + ", ".join(pending) + f". {context}"
        )
    lines.append("")

    lines += ["## Recent sessions", ""]
    ran = [a for a in activity if a.entries is not None]
    if not ran:
        lines.append("No earlier sessions on record.")
    else:
        for a in activity:
            text = "no session" if a.entries is None else f"{a.entries} entries"
            lines.append(f"* {a.day:%a %Y-%m-%d}: {text}")
        idle = sum(1 for a in ran if a.entries == 0)
        lines += [
            "",
            f"Sessions without a trade: {idle} of the last {len(ran)}. In normal conditions "
            "that should be at most about one a week, so keep looking for opportunities.",
        ]
    lines.append("")

    lines += _learning_lines(config, lessons, record)
    return "\n".join(lines) + "\n"


def _learning_lines(config: AppConfig, book: LessonBook | None, record: Record | None) -> list[str]:
    rules = config.learning
    shown = rules.briefing_lessons
    lines = ["## Lessons and your record", ""]
    active = book.by_status(LessonStatus.ACTIVE) if book else []
    candidates = book.by_status(LessonStatus.CANDIDATE) if book else []
    if not active and not candidates:
        lines += [
            "No lessons yet. They come from your daily reviews: a lesson starts as a "
            f"candidate and is active once {rules.min_support} reviewed decisions support it.",
            "",
        ]
    if active:
        lines += [
            f"Active (at least {rules.min_support} reviewed decisions support each, little "
            "against). Use them, but today's market overrules them when it disagrees:",
            "",
        ]
        lines += [_lesson_line(lsn) for lsn in active[:shown]]
        lines.append("")
    if candidates:
        lines += ["Candidates (unproven: watch for evidence, do not rely on them):", ""]
        lines += [_lesson_line(lsn) for lsn in candidates[:shown]]
        lines.append("")
    if record is None or not record.sessions:
        lines.append("No earlier session journals for a record yet.")
        return lines
    lines.append(f"Results by read over the last {len(record.sessions)} sessions:")
    lines.append("")
    if record.reads:
        lines += [
            "| Read (`setup`) | Trades | Net R | Average R | Win rate |",
            "| - | - | - | - | - |",
        ]
        lines += [
            f"| {s.setup} | {s.trades} | {s.net_r:+.2f} | {s.average_r:+.2f} "
            f"| {s.wins / s.trades:.0%} |"
            for s in record.reads
        ]
    else:
        lines.append("No closed trades yet.")
    if record.calibration:
        lines += [
            "",
            "Calibration (probability you gave the chosen scenario, and how often it played out):",
            "",
        ]
        lines += [
            f"* {b.label}: {b.decisions} decisions, stated {b.stated:.0%} on average, "
            f"played out {b.realized:.0%}"
            for b in record.calibration
        ]
    if record.unreviewed:
        lines += ["", f"{record.unreviewed} entries in these sessions were never reviewed."]
    return lines


def _lesson_line(lesson: Lesson) -> str:
    added = lesson.added
    tags = [
        f"{name} {value}"
        for name in ("setup", "regime", "symbol")
        if (value := getattr(added, name))
    ]
    tag_text = f"{', '.join(tags)}; " if tags else ""
    return (
        f"* `{lesson.lesson_id}` {added.statement} ({tag_text}{lesson.supports} for, "
        f"{lesson.contradicts} against)"
    )


def _pct(value: Any) -> str:
    return "unset" if value is None else f"{(value * 100).normalize():f}%"
