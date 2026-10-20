import asyncio

import pytest

from perps_agent.venue import RateLimited, RequestRejected, VenueUnavailable
from perps_agent.venue.retry import RetryPolicy, with_retries


def run(call, policy, sleeps):
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return asyncio.run(with_retries(call, policy, operation="test", sleep=sleep))


def test_backoff_doubles_and_is_capped() -> None:
    policy = RetryPolicy(attempts=5, base_delay_seconds=1, max_delay_seconds=3)
    sleeps: list[float] = []

    async def failing():
        raise VenueUnavailable("down")

    with pytest.raises(VenueUnavailable):
        run(failing, policy, sleeps)
    assert sleeps == [1, 2, 3, 3]


def test_server_retry_hint_is_honoured_within_cap() -> None:
    policy = RetryPolicy(attempts=2, base_delay_seconds=0.5, max_delay_seconds=8)
    sleeps: list[float] = []
    attempts = 0

    async def limited():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimited("slow down", retry_after=30)
        return "ok"

    assert run(limited, policy, sleeps) == "ok"
    assert sleeps == [8]


def test_rejections_are_not_retried() -> None:
    sleeps: list[float] = []

    async def rejected():
        raise RequestRejected("bad", status=400)

    with pytest.raises(RequestRejected):
        run(rejected, RetryPolicy(), sleeps)
    assert sleeps == []


def test_policy_needs_an_attempt() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)
