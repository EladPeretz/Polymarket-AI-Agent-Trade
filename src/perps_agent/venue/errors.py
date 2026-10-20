"""Venue errors. SDK and HTTP exceptions never leave ``perps_agent.venue``."""


class VenueError(Exception):
    """Base class for venue failures."""


class VenueUnavailable(VenueError):
    """Network failure, timeout, or a 5xx/503 (e.g. tickers staler than 10 s).

    For reads it is safe to retry. A 5xx does not prove a write did not execute.
    """


class RateLimited(VenueError):
    """HTTP 429 / ``ip_rate_limited``. ``retry_after`` is the server's hint, in seconds."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RequestRejected(VenueError):
    """A non-retryable 4xx. ``code`` is the venue's machine-readable error, if given."""

    def __init__(self, message: str, *, status: int, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class SchemaMismatch(VenueError):
    """A response did not match the expected shape. Usually an API change: update
    fixtures and the adapter, and do not retry."""


class UnknownInstrument(VenueError):
    """No instrument with this symbol or id is listed."""
