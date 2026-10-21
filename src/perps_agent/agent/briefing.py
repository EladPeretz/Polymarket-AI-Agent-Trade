"""The agent's system prompt and session briefing (overview §3.13, §4).

The system prompt (``prompts/system.md``) is fixed: the role, the decision
process and the owner's trading style. The briefing is built each morning
from the config and the results store: the day's window, the symbols and
their notes, the risk limits, the playbooks' Gate 1 status and how many recent
sessions had no trade (owner rule: at most about one a week). Lessons and
calibration statistics join it with the learning loop (WP14).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from perps_agent.calendar import NyseCalendar, TradingWindow
from perps_agent.config.models import AppConfig
from perps_agent.journal import Action, Journal, journal_path, load_journal
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
) -> str:
    zone = config.session.zone
    screened = frozenset(screened)
    risk = config.risk
    lines = [f"# Briefing for {day:%A %Y-%m-%d}", ""]
    if window is None:
        lines += ["Not a trading day: no window today.", ""]
    else:
        lines += [
            "## Window (New York)",
            "",
            f"* Trading window {window.start.astimezone(zone):%H:%M}–"
            f"{window.end.astimezone(zone):%H:%M}; entries until "
            f"{window.entry_cutoff.astimezone(zone):%H:%M}; everything is flat by "
            f"{window.end.astimezone(zone):%H:%M}.",
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
        f"* Full size risks {_pct(risk.per_trade_risk_fraction)} of equity per trade "
        f"(L0/L1); discretionary (L2) trades use {_pct(risk.l2_risk_fraction)} of that.",
        f"* Daily loss limit {_pct(risk.daily_loss_limit_fraction)} of start-of-day equity, "
        "counting what open positions would lose at their stops. It is the only stop for "
        "the day.",
        f"* At most {risk.max_concurrent_positions} positions at once, one per symbol; "
        f"{risk.max_trades_per_day} entries a day, of which {risk.max_l2_trades_per_day} L2. "
        "An entry that is cancelled without any fill gives its slot back.",
        f"* Stops at least the 15-minute range from entry (never under "
        f"{risk.min_stop_distance_bp} bp); a target, if set, at least {risk.min_reward_risk}R "
        "after costs.",
        "",
    ]

    lines += ["## Playbooks", ""]
    passed = [p for p in playbooks if p.tag in screened]
    if passed:
        lines.append("Screened (may trade as L0/L1): " + ", ".join(p.tag for p in passed) + ".")
    pending = [p.id for p in playbooks if p.tag not in screened]
    if pending:
        context = (
            "Their setups still show up, marked unscreened: context only, act on one as a "
            "discretionary (L2) trade if you want it."
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
            "that should be at most about one a week, so keep looking for valid setups.",
        ]
    lines.append("")

    lines += [
        "## Lessons and calibration",
        "",
        "None yet: the learning loop (lessons notebook, calibration statistics and the "
        "`simulate` tool) arrives with WP14.",
    ]
    return "\n".join(lines) + "\n"


def _pct(value: Any) -> str:
    return "unset" if value is None else f"{(value * 100).normalize():f}%"
