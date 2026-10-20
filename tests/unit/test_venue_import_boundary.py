"""ADR-0001: only ``perps_agent.venue`` may import the ``polymarket`` SDK."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "perps_agent"
# Within the venue package, only the adapter module talks to the SDK.
ALLOWED = {SRC / "venue" / "polymarket_source.py"}


def _imports_sdk(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(name == "polymarket" or name.startswith("polymarket.") for name in names):
            lines.append(node.lineno)
    return lines


def test_only_the_venue_adapter_imports_the_sdk() -> None:
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in SRC.rglob("*.py")
        if path not in ALLOWED and (lines := _imports_sdk(path))
    }
    assert offenders == {}, f"polymarket SDK imported outside the venue adapter: {offenders}"


def test_the_adapter_still_exists() -> None:
    assert all(path.exists() and _imports_sdk(path) for path in ALLOWED)


def test_loading_the_venue_package_does_not_load_the_sdk() -> None:
    import subprocess
    import sys

    code = "import sys, perps_agent.venue; print('polymarket' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
