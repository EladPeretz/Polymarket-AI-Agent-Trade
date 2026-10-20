from datetime import UTC, datetime, timedelta, timezone

import pytest

from perps_agent.clock import from_ms, to_ms, to_utc, utc_now


def test_utc_now_is_aware_utc():
    assert utc_now().tzinfo is UTC


def test_to_utc_converts_aware_and_rejects_naive():
    eastern = timezone(timedelta(hours=-4))
    assert to_utc(datetime(2026, 10, 19, 9, 30, tzinfo=eastern)) == datetime(
        2026, 10, 19, 13, 30, tzinfo=UTC
    )
    with pytest.raises(ValueError):
        to_utc(datetime(2026, 10, 19, 9, 30))


@pytest.mark.parametrize("ms", [0, 1, 1792406580488, 1792406580489])
def test_ms_round_trip_is_exact(ms):
    assert to_ms(from_ms(ms)) == ms


def test_to_ms_truncates_microseconds():
    value = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(microseconds=1999)
    assert to_ms(value) == to_ms(datetime(2026, 1, 1, tzinfo=UTC)) + 1
