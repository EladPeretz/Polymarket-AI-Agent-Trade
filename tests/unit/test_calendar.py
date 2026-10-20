import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from perps_agent.app.cli import EXIT_NOT_TRADING_DAY, main
from perps_agent.calendar import (
    CalendarError,
    MarketSession,
    NyseCalendar,
    TradingCalendar,
    early_closes,
    holidays,
)
from perps_agent.config import SessionConfig
from tests.conftest import REPO_ROOT

NY = ZoneInfo("America/New_York")

# NYSE's published holiday and early-close lists.
PUBLISHED_HOLIDAYS = {
    2025: [
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
        "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    ],
    2026: [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    ],
    2027: [
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    ],
}  # fmt: skip
PUBLISHED_EARLY_CLOSES = {
    2025: ["2025-07-03", "2025-11-28", "2025-12-24"],
    2026: ["2026-11-27", "2026-12-24"],
    2027: ["2027-11-26"],
}


def ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


@pytest.mark.parametrize("year", sorted(PUBLISHED_HOLIDAYS))
def test_holidays_match_nyse_published_list(year):
    assert sorted(d.isoformat() for d in holidays(year)) == PUBLISHED_HOLIDAYS[year]


@pytest.mark.parametrize("year", sorted(PUBLISHED_EARLY_CLOSES))
def test_early_closes_match_nyse_published_list(year):
    assert sorted(d.isoformat() for d in early_closes(year)) == PUBLISHED_EARLY_CLOSES[year]


def test_new_year_on_saturday_is_not_observed_on_friday():
    # 2022-01-01 was a Saturday; NYSE stayed open on Friday 2021-12-31 and 2022-01-03.
    assert date(2022, 1, 1) not in holidays(2022)
    assert date(2022, 1, 3) not in holidays(2022)


def test_new_year_on_sunday_moves_to_monday():
    assert holidays(2023)[date(2023, 1, 2)] == "New Year's Day"


def test_special_and_configured_closures():
    calendar = NyseCalendar(extra_closures=[date(2026, 10, 21)])
    assert calendar.closure_reason(date(2025, 1, 9)) is not None  # President Carter
    assert calendar.closure_reason(date(2026, 10, 21)) == "unscheduled closure (config)"
    assert NyseCalendar().is_trading_day(date(2026, 10, 21))


def test_dates_outside_verified_years_fail_loudly():
    with pytest.raises(CalendarError):
        NyseCalendar().is_trading_day(date(2031, 1, 2))
    with pytest.raises(CalendarError):
        holidays(2021)


def test_trading_day_hours_in_utc_across_dst():
    calendar = NyseCalendar()
    winter = calendar.trading_day(date(2026, 1, 5))
    summer = calendar.trading_day(date(2026, 7, 6))
    assert winter is not None and summer is not None
    assert winter.open == datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    assert winter.close == datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    assert summer.open == datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
    assert calendar.trading_day(date(2026, 7, 3)) is None
    early = calendar.trading_day(date(2026, 12, 24))
    assert early is not None and early.early_close
    assert early.close == ny(2026, 12, 24, 13)


def test_next_and_previous_trading_day_skip_weekends_and_holidays():
    calendar = NyseCalendar()
    assert calendar.next_trading_day(date(2026, 7, 2)) == date(2026, 7, 6)
    assert calendar.previous_trading_day(date(2026, 1, 20)) == date(2026, 1, 16)


@pytest.mark.parametrize(
    ("now", "session"),
    [
        (ny(2026, 10, 20, 3, 59), MarketSession.OVERNIGHT),
        (ny(2026, 10, 20, 4), MarketSession.PREMARKET),
        (ny(2026, 10, 20, 9, 29), MarketSession.PREMARKET),
        (ny(2026, 10, 20, 9, 30), MarketSession.REGULAR),
        (ny(2026, 10, 20, 15, 59), MarketSession.REGULAR),
        (ny(2026, 10, 20, 16), MarketSession.POSTMARKET),
        (ny(2026, 10, 20, 20), MarketSession.OVERNIGHT),
        (ny(2026, 10, 23, 20), MarketSession.WEEKEND),  # Friday evening
        (ny(2026, 10, 25, 12), MarketSession.WEEKEND),  # Sunday
        (ny(2026, 10, 26, 3), MarketSession.WEEKEND),  # early Monday
        (ny(2026, 11, 26, 12), MarketSession.CLOSED),  # Thanksgiving
        (ny(2026, 11, 25, 21), MarketSession.CLOSED),  # night before Thanksgiving
        (ny(2026, 11, 27, 13), MarketSession.POSTMARKET),  # early close
        (ny(2026, 11, 27, 17), MarketSession.WEEKEND),
        (ny(2026, 1, 16, 21), MarketSession.CLOSED),  # weekend + MLK Monday
    ],
)
def test_session_at(now, session):
    assert NyseCalendar().session_at(now) == session


def test_session_at_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        NyseCalendar().session_at(datetime(2026, 10, 20, 12))


def test_trading_window_on_a_regular_day():
    window = TradingCalendar(SessionConfig()).trading_window(date(2026, 10, 20))
    assert window is not None
    assert window.start == ny(2026, 10, 20, 9, 30)
    assert window.entry_cutoff == ny(2026, 10, 20, 12, 15)
    assert window.end == ny(2026, 10, 20, 12, 30)
    assert window.hard_flat_deadline == ny(2026, 10, 20, 12, 35)
    assert window.start.tzinfo is UTC


def test_no_window_on_non_trading_days():
    calendar = TradingCalendar(SessionConfig())
    assert calendar.trading_window(date(2026, 10, 24)) is None
    assert calendar.trading_window(date(2026, 12, 25)) is None


def test_extra_closures_from_config_remove_the_window():
    config = SessionConfig.model_validate({"extra_closures": ["2026-10-21"]})
    assert TradingCalendar(config).trading_window(date(2026, 10, 21)) is None


def test_window_is_capped_at_an_early_close():
    config = SessionConfig.model_validate(
        {"window_start": "11:00", "entry_cutoff": "12:45", "window_end": "13:15",
         "hard_flat_deadline": "13:30"}
    )  # fmt: skip
    window = TradingCalendar(config).trading_window(date(2026, 11, 27))
    assert window is not None
    assert window.entry_cutoff == ny(2026, 11, 27, 12, 45)
    assert window.end == window.hard_flat_deadline == ny(2026, 11, 27, 13)


def test_no_window_when_it_would_start_after_an_early_close():
    config = SessionConfig.model_validate(
        {"window_start": "13:30", "entry_cutoff": "14:00", "window_end": "14:30",
         "hard_flat_deadline": "14:35"}
    )  # fmt: skip
    calendar = TradingCalendar(config)
    assert calendar.trading_window(date(2026, 11, 27)) is None
    assert calendar.trading_window(date(2026, 11, 30)) is not None
    assert calendar.must_be_flat(ny(2026, 11, 27, 13, 45))


@pytest.mark.parametrize(
    ("now", "entry", "flat"),
    [
        (ny(2026, 10, 20, 9, 29), False, True),
        (ny(2026, 10, 20, 9, 30), True, False),
        (ny(2026, 10, 20, 12, 14), True, False),
        (ny(2026, 10, 20, 12, 15), False, False),
        (ny(2026, 10, 20, 12, 29), False, False),
        (ny(2026, 10, 20, 12, 30), False, True),
        (ny(2026, 10, 20, 15), False, True),
        (ny(2026, 10, 24, 10), False, True),  # Saturday
        (ny(2026, 11, 26, 10), False, True),  # Thanksgiving
    ],
)
def test_entry_and_flat_rules(now, entry, flat):
    calendar = TradingCalendar(SessionConfig())
    assert calendar.is_entry_allowed(now) is entry
    assert calendar.must_be_flat(now) is flat
    assert calendar.is_entry_allowed(now.astimezone(UTC)) is entry


def test_every_minute_of_a_day_is_consistent():
    """Entries are only allowed inside the window; flat is required everywhere else."""
    calendar = TradingCalendar(SessionConfig())
    start = ny(2026, 11, 27, 0)
    for minute in range(24 * 60):
        now = start + timedelta(minutes=minute)
        if calendar.is_entry_allowed(now):
            assert not calendar.must_be_flat(now)
        window = calendar.window_at(now)
        assert window is not None
        assert calendar.must_be_flat(now) is not (window.start <= now < window.end)


def test_cli_prints_window_for_trading_day(capsys):
    config = str(REPO_ROOT / "config" / "default.yaml")
    assert main(["calendar", "--config", config, "--date", "2026-11-27"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["trading_day"] is True
    assert report["early_close"] is True
    assert report["window_start"] == "2026-11-27T14:30:00+00:00"


def test_cli_exit_code_on_non_trading_day(capsys):
    config = str(REPO_ROOT / "config" / "default.yaml")
    assert main(["calendar", "--config", config, "--date", "2026-11-26"]) == EXIT_NOT_TRADING_DAY
    report = json.loads(capsys.readouterr().out)
    assert report == {"date": "2026-11-26", "trading_day": False, "closed": "Thanksgiving Day"}


def test_cli_reports_unsupported_year(capsys):
    config = str(REPO_ROOT / "config" / "default.yaml")
    assert main(["calendar", "--config", config, "--date", "2040-01-03"]) == 1
    assert "error:" in capsys.readouterr().err
