"""Instrument registry: the venue's instrument list, loaded at startup and refreshed.

Venue parameters (precision, limits, price band, leverage tiers, close-only) are
always read from here, never hard-coded. The venue changes them without notice,
so every refresh logs what changed.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from perps_agent.clock import utc_now
from perps_agent.venue.errors import UnknownInstrument, VenueError
from perps_agent.venue.interfaces import MarketDataSource
from perps_agent.venue.models import Instrument

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegistryChange:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


class InstrumentRegistry:
    def __init__(self, source: MarketDataSource) -> None:
        self._source = source
        self._by_symbol: dict[str, Instrument] = {}
        self._by_id: dict[int, Instrument] = {}
        self._loaded_at: datetime | None = None

    @property
    def loaded_at(self) -> datetime | None:
        return self._loaded_at

    def __len__(self) -> int:
        return len(self._by_symbol)

    async def refresh(self) -> RegistryChange:
        instruments = await self._source.fetch_instruments()
        new = {i.symbol: i for i in instruments}
        if len(new) != len(instruments) or len({i.instrument_id for i in instruments}) != len(
            instruments
        ):
            raise VenueError("instrument list has duplicate symbols or ids")
        old = self._by_symbol
        change = RegistryChange(
            added=tuple(sorted(new.keys() - old.keys())),
            removed=tuple(sorted(old.keys() - new.keys())),
            changed=tuple(sorted(s for s in new.keys() & old.keys() if new[s] != old[s])),
        )
        self._by_symbol = new
        self._by_id = {i.instrument_id: i for i in instruments}
        self._loaded_at = utc_now()
        if change and old:
            log.warning(
                "instrument parameters changed",
                extra={
                    "added": change.added,
                    "removed": change.removed,
                    "changed": change.changed,
                },
            )
        return change

    def by_symbol(self, symbol: str) -> Instrument:
        try:
            return self._by_symbol[symbol]
        except KeyError:
            raise UnknownInstrument(f"no instrument {symbol!r}") from None

    def by_id(self, instrument_id: int) -> Instrument:
        try:
            return self._by_id[instrument_id]
        except KeyError:
            raise UnknownInstrument(f"no instrument with id {instrument_id}") from None

    async def run_refresh(self, interval_seconds: float, stop: asyncio.Event) -> None:
        """Refresh every ``interval_seconds`` until ``stop`` is set.

        A failed refresh is logged and keeps the last good list.
        """
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                pass
            try:
                await self.refresh()
            except VenueError as error:
                log.warning("instrument refresh failed", extra={"error": str(error)})
