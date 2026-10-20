"""Bounded retries with exponential backoff for idempotent venue reads.

Only reads go through here. A failed write is never blindly retried: its outcome
is ambiguous and must be reconciled by client order id (venue research §13).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from perps_agent.venue.errors import RateLimited, VenueUnavailable

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")

    def delay(self, attempt: int, error: Exception) -> float:
        """Delay before retry number ``attempt`` (1-based); honours a server hint, capped."""
        backoff = self.base_delay_seconds * float(2 ** (attempt - 1))
        if isinstance(error, RateLimited) and error.retry_after is not None:
            backoff = max(backoff, error.retry_after)
        return min(backoff, self.max_delay_seconds)


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    operation: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``call``, retrying ``VenueUnavailable`` and ``RateLimited`` up to the policy's limit."""
    for attempt in range(1, policy.attempts + 1):
        try:
            return await call()
        except (VenueUnavailable, RateLimited) as error:
            if attempt == policy.attempts:
                raise
            delay = policy.delay(attempt, error)
            log.warning(
                "venue read failed, retrying",
                extra={
                    "operation": operation,
                    "attempt": attempt,
                    "delay_s": delay,
                    "error": str(error),
                },
            )
            await sleep(delay)
    raise AssertionError("unreachable")
