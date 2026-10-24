"""The daily report as Markdown (``report.md``) and JSON (``report.json``).

Incidents come first: they are the session's alerts in paper mode (owner
decision 2026-10-21). Times are shown in New York time.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from perps_agent.report.daily import REPORT_JSON, REPORT_MD, Attribution, DailyReport, build_report

_ZONE = ZoneInfo("America/New_York")


def render_markdown(report: DailyReport) -> str:
    lines = [f"# Daily report {report.day.isoformat()}", ""]
    meta = [f"mode {report.mode}" if report.mode else None]
    if report.symbols:
        meta.append("symbols " + ", ".join(report.symbols))
    meta.append(f"generated {_time(report.generated_at)} New York")
    lines += [" · ".join(m for m in meta if m), ""]

    lines += ["## Incidents", ""]
    if report.incidents:
        lines += _table(
            ["Time", "Severity", "Kind", "Detail"],
            [
                [_time(i.at), i.severity.upper(), str(i.kind), _cell(i.detail)]
                for i in report.incidents
            ],
        )
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Result", ""]
    rows = [
        ["Start equity", _money(report.start_equity)],
        ["End equity", _money(report.end_equity)],
        ["Return", _pct(report.return_fraction)],
        ["Gross PnL", _money(report.gross_pnl, signed=True)],
        ["Fees", _money(-report.fees, signed=True)],
        ["Funding", _money(report.funding, signed=True)],
        ["Net PnL", _money(report.net_pnl, signed=True)],
        [
            "Max drawdown",
            f"{_money(report.max_drawdown)} ({_pct(report.max_drawdown_fraction, signed=False)})",
        ],
        ["Trades", str(len(report.trades))],
    ]
    lines += _table(["", ""], rows)
    if report.open_at_end:
        lines += [
            "",
            "**Open at the end of the session:** "
            + ", ".join(
                f"{p.symbol} {p.direction} {p.quantity} @ {_price(p.average_entry)}"
                for p in report.open_at_end
            ),
        ]
    lines.append("")

    lines += ["## Trades", ""]
    if report.trades:
        lines += _table(
            ["Open", "Close", "Symbol", "Side", "Level", "Source", "Entry", "Exit", "Net", "R",
             "Exit reason"],
            [
                [
                    _time(t.opened_at),
                    _time(t.closed_at),
                    t.symbol,
                    str(t.direction),
                    t.level,
                    t.source,
                    _price(t.average_entry),
                    _price(t.average_exit),
                    _money(t.net_pnl, signed=True),
                    _r(t.r_multiple),
                    _cell(t.exit_reason or ""),
                ]
                for t in report.trades
            ],
        )  # fmt: skip
    else:
        lines.append("No trades.")
    lines.append("")

    for title, rows_ in (
        ("By autonomy level", report.by_level),
        ("By source", report.by_source),
        ("By symbol", report.by_symbol),
    ):
        if rows_:
            lines += [f"## {title}", "", *_attribution(rows_), ""]

    lines += ["## Decisions and risk", ""]
    decisions = ", ".join(f"{k} {v}" for k, v in sorted(report.decisions.items())) or "none"
    verdicts = ", ".join(f"{k} {v}" for k, v in sorted(report.verdicts.items())) or "none"
    lines += [f"* Decisions: {decisions}", f"* Risk verdicts: {verdicts}"]
    if report.failures:
        lines.append(f"* Execution refused {len(report.failures)} approved action(s)")
    lines.append("")
    for title, verdict_rows in (("Rejections", report.rejections), ("Resizes", report.resizes)):
        if verdict_rows:
            lines += [f"### {title}", ""]
            lines += _table(
                ["Time", "Symbol", "Level", "Checks", "Detail"],
                [
                    [_time(v.at), v.symbol, v.level, ", ".join(v.checks), _cell(v.detail)]
                    for v in verdict_rows
                ],
            )
            lines.append("")
    if report.failures:
        lines += ["### Execution refusals", ""]
        lines += _table(
            ["Time", "Symbol", "Error"],
            [[_time(f.at), f.symbol or "", _cell(f.error)] for f in report.failures],
        )
        lines.append("")

    latency = report.latency
    lines += ["## Agent latency", ""]
    if latency.events:
        lines += [
            f"* Events delivered: {latency.events}, answered: {latency.answered}",
            f"* Pickup (event to delivery): median {_seconds(latency.pickup_median_seconds)}, "
            f"max {_seconds(latency.pickup_max_seconds)}",
            f"* Answer (delivery to next call): median {_seconds(latency.answer_median_seconds)}, "
            f"p90 {_seconds(latency.answer_p90_seconds)}, "
            f"max {_seconds(latency.answer_max_seconds)}",
        ]
    else:
        lines.append("No events were delivered to the agent.")
    lines.append("")
    return "\n".join(lines)


def write_report(day_dir: Path) -> DailyReport:
    """Build the day's report and write ``report.md`` and ``report.json`` next to its data."""
    report = build_report(day_dir)
    (day_dir / REPORT_MD).write_text(render_markdown(report), encoding="utf-8")
    (day_dir / REPORT_JSON).write_text(report.model_dump_json(indent=1) + "\n", encoding="utf-8")
    return report


def _attribution(rows: Sequence[Attribution]) -> list[str]:
    return _table(
        ["", "Trades", "Wins", "Net", "Total R", "Avg R"],
        [
            [
                a.key,
                str(a.trades),
                str(a.wins),
                _money(a.net_pnl, signed=True),
                _r(a.total_r),
                _r(a.average_r),
            ]
            for a in rows
        ],
    )


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("-" for _ in header) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _time(at: datetime) -> str:
    return at.astimezone(_ZONE).strftime("%H:%M:%S")


def _price(value: Decimal) -> str:
    """Average prices of several fills, to 4 decimal places without trailing zeros."""
    return format(value.quantize(Decimal("0.0001")).normalize(), "f")


def _money(value: Decimal | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:+,.2f}" if signed else f"{value:,.2f}"


def _pct(value: Decimal | None, *, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.2f}%" if signed else f"{value * 100:.2f}%"


def _r(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}R"


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}s"
