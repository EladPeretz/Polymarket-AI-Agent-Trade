from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

COMPLETE_RISK = {
    "per_trade_risk_fraction": 0.005,
    "daily_loss_limit_fraction": 0.02,
    "max_leverage": 3,
    "max_position_notional": 5000,
    "max_trades_per_day": 6,
    "max_l2_trades_per_day": 2,
    "min_stop_distance_bp": 10,
    "max_spread_bp": 8,
    "min_top_of_book_notional": 2000,
    "liquidation_buffer_fraction": 0.5,
}

COMPLETE_SIM = {"starting_collateral": 10000}


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a main config plus symbol files into tmp_path and return the main path."""

    def _write(main: dict, symbols: dict[str, dict] | None = None) -> Path:
        symbols = {"NVDA-USD": {"symbol": "NVDA-USD"}} if symbols is None else symbols
        (tmp_path / "symbols").mkdir(exist_ok=True)
        for name, body in symbols.items():
            (tmp_path / "symbols" / f"{name}.yaml").write_text(yaml.safe_dump(body))
        path = tmp_path / "default.yaml"
        path.write_text(yaml.safe_dump({"symbols": list(symbols), **main}))
        return path

    return _write
