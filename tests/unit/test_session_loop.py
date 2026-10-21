import asyncio
from typing import Any

from perps_agent.app import session


class _Engine:
    async def pump(self) -> None: ...

    async def step(self, now: Any) -> None: ...


class _Sampler:
    async def sample(self, now: Any, force: bool = False) -> None: ...


class _Guardian:
    def __init__(self) -> None:
        self.flat = False

    async def check(self, now: Any) -> None: ...

    def session_over(self, now: Any) -> bool:
        return False


class _Execution:
    def __init__(self, guardian: _Guardian) -> None:
        self.guardian = guardian
        self.holds: list[str] = []
        self.closed: list[str] = []

    def hold_entries(self, key: str, reason: str) -> None:
        self.holds.append(key)

    async def close_all(self, reason: str) -> None:
        self.closed.append(reason)
        self.guardian.flat = True


def test_an_early_end_holds_entries_closes_everything_and_ends_the_loop():
    guardian = _Guardian()
    execution = _Execution(guardian)
    end_early = asyncio.Event()
    end_early.set()

    async def run() -> None:
        await asyncio.wait_for(
            session._loop(
                _Engine(),  # type: ignore[arg-type]
                guardian,  # type: ignore[arg-type]
                _Sampler(),  # type: ignore[arg-type]
                asyncio.Event(),
                execution,  # type: ignore[arg-type]
                end_early,
            ),
            timeout=5,
        )

    asyncio.run(run())
    assert execution.holds == [session.EARLY_END_HOLD]
    assert len(execution.closed) == 1
