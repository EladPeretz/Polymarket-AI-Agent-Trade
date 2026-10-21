"""Equity samples: the session's equity once a minute, for the report's drawdown.

Stored as ``results/<date>/equity.jsonl``. Equity is the ledger's collateral
plus unrealized PnL at the latest marks.
"""

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

from perps_agent.venue.models import UtcDatetime

EQUITY_FILE = "equity.jsonl"


class EquitySample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: UtcDatetime
    equity: Decimal
    open_positions: int


_ADAPTER: TypeAdapter[EquitySample] = TypeAdapter(EquitySample)


class JsonlEquitySink:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def __call__(self, sample: EquitySample) -> None:
        self._file.write(_ADAPTER.dump_json(sample).decode() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def load_equity(path: Path) -> list[EquitySample]:
    with path.open(encoding="utf-8") as lines:
        return [_ADAPTER.validate_json(line) for line in lines if line.strip()]


def max_drawdown(start: Decimal, samples: list[EquitySample]) -> Decimal:
    """Largest peak-to-trough fall in pUSD, the peak starting at ``start``."""
    peak, worst = start, Decimal(0)
    for sample in samples:
        peak = max(peak, sample.equity)
        worst = max(worst, peak - sample.equity)
    return worst
