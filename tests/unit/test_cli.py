import json

import yaml

from perps_agent.app.cli import main
from tests.conftest import COMPLETE_RISK, COMPLETE_SIM, REPO_ROOT


def test_repo_defaults_are_ready_for_paper_mode(capsys):
    path = str(REPO_ROOT / "config" / "default.yaml")
    assert main(["config-check", "--config", path, "--strict"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["mode"] == "paper"
    assert "ready for paper mode" in err


def test_config_check_warns_but_succeeds_when_not_ready(write_config, capsys):
    path = write_config({})
    assert main(["config-check", "--config", str(path)]) == 0
    assert "warning: not ready for paper mode" in capsys.readouterr().err


def test_config_check_strict_fails_when_not_ready(write_config):
    path = write_config({})
    assert main(["config-check", "--config", str(path), "--strict"]) == 1


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


def test_kill_creates_and_clears_the_switch_file(write_config, tmp_path, capsys):
    switch = tmp_path / "KILL"
    path = write_config({"guardian": {"kill_switch_file": str(switch)}})
    assert main(["kill", "--config", str(path), "--reason", "owner stop"]) == 0
    assert "owner stop" in switch.read_text()
    assert main(["kill", "--config", str(path), "--clear"]) == 0
    assert not switch.exists()


def test_report_builds_from_the_results_directory(write_config, tmp_path, capsys):
    results = tmp_path / "results"
    (results / "2026-10-20").mkdir(parents=True)
    path = write_config({"agent": {"results_dir": str(results)}})
    assert main(["report", "--config", str(path), "--date", "2026-10-20"]) == 0
    assert "# Daily report 2026-10-20" in capsys.readouterr().out
    assert (results / "2026-10-20" / "report.json").exists()
    assert main(["report", "--config", str(path), "--date", "2026-10-19"]) == 1


def test_a_failed_publish_is_recorded_as_an_incident(write_config, tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "missing.git")], cwd=repo, check=True
    )
    results = repo / "results"
    (results / "2026-10-20").mkdir(parents=True)
    (results / "2026-10-20" / "report.md").write_text("# report\n")
    path = write_config({"agent": {"results_dir": str(results)}, "results": {"push_attempts": 1}})
    monkeypatch.chdir(repo)
    assert main(["publish", "--config", str(path)]) == 1
    incident = json.loads((results / "2026-10-20" / "incidents.jsonl").read_text())
    assert incident["kind"] == "publish_failed" and incident["severity"] == "critical"
