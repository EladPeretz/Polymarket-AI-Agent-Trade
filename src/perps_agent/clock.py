"""Time helpers. All internal timestamps are timezone-aware UTC datetimes."""

from datetime import UTC, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC. Naive datetimes are rejected."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime is not allowed: {value!r}")
    return value.astimezone(UTC)


def from_ms(ms: int) -> datetime:
    """Convert venue Unix milliseconds to a UTC datetime (exact, no float rounding)."""
    return _EPOCH + ms * _MS


def to_ms(value: datetime) -> int:
    """Convert an aware datetime to Unix milliseconds, truncating sub-millisecond parts."""
    return (to_utc(value) - _EPOCH) // _MS
