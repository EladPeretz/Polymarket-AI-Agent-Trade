import json

import yaml

from perps_agent.app.cli import main
from tests.conftest import COMPLETE_RISK, COMPLETE_SIM, REPO_ROOT


def test_config_check_warns_but_succeeds_on_repo_defaults(capsys):
    assert main(["config-check", "--config", str(REPO_ROOT / "config" / "default.yaml")]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["mode"] == "paper"
    assert "warning: not ready for paper mode" in err


def test_config_check_strict_fails_when_not_ready():
    path = str(REPO_ROOT / "config" / "default.yaml")
    assert main(["config-check", "--config", path, "--strict"]) == 1


def test_config_check_strict_passes_when_ready(write_config, capsys):
    path = write_config({"risk": COMPLETE_RISK, "sim": COMPLETE_SIM})
    assert main(["config-check", "--config", str(path), "--strict"]) == 0
    assert "ready for paper mode" in capsys.readouterr().err


def test_config_check_reports_invalid_config(tmp_path, capsys):
    path = tmp_path / "default.yaml"
    path.write_text(yaml.safe_dump({"symbols": "NVDA-USD"}))
    assert main(["config-check", "--config", str(path)]) == 1
    assert "error:" in capsys.readouterr().err


def test_record_reports_invalid_config(tmp_path, capsys):
    bad = tmp_path / "default.yaml"
    bad.write_text("symbols: nope\n")
    assert main(["record", "--config", str(bad), "--seconds", "1"]) == 1
    assert "error:" in capsys.readouterr().err
