import json

import yaml

from perps_agent.app.cli import main
from perps_agent.config import load_config
from perps_agent.strategy.playbooks import load_playbooks
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


def test_config_check_flags_an_exempt_playbook_that_no_longer_exists(write_config, capsys):
    path = write_config(
        {
            "risk": COMPLETE_RISK,
            "sim": COMPLETE_SIM,
            "agent": {"exempt_playbooks": ["vwap_reversion@1.00000000"]},
        }
    )
    assert main(["config-check", "--config", str(path), "--strict"]) == 1
    assert "vwap_reversion@1.00000000 is not a current playbook version" in capsys.readouterr().err


def test_the_default_config_exempts_the_current_playbook_versions():
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    assert set(config.agent.exempt_playbooks) == {p.tag for p in load_playbooks()}


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


def test_backtest_refuses_unknown_symbols_and_bad_params(write_config, tmp_path, capsys):
    path = str(write_config({"risk": COMPLETE_RISK, "sim": COMPLETE_SIM}))
    assert main(["backtest", "--config", path, "--symbol", "AAPL-USD"]) == 1
    assert "unknown symbols: AAPL-USD" in capsys.readouterr().err
    params = tmp_path / "params.yaml"
    params.write_text(yaml.safe_dump({"momentum_scalp": {"target_r": 9}}))
    assert main(["backtest", "--config", path, "--params", str(params)]) == 1
    assert "momentum_scalp" in capsys.readouterr().err
    params.write_text("[1, 2]")
    assert main(["backtest", "--config", path, "--params", str(params)]) == 1


def test_backtest_needs_capital_values(write_config, capsys):
    assert main(["backtest", "--config", str(write_config({}))]) == 1
    assert "not ready for backtest mode" in capsys.readouterr().err


def test_prompt_lists_gate1_passes_as_screened(write_config, tmp_path, capsys):
    tag = next(p.tag for p in load_playbooks() if p.id == "momentum_scalp")
    results = tmp_path / "results"
    (results / "gate1").mkdir(parents=True)
    (results / "gate1" / f"{tag}.json").write_text(json.dumps({"tag": tag, "verdict": "pass"}))
    path = write_config(
        {"risk": COMPLETE_RISK, "sim": COMPLETE_SIM, "agent": {"results_dir": str(results)}}
    )
    assert main(["prompt", "--config", str(path), "--date", "2026-10-20"]) == 0
    out = capsys.readouterr().out
    assert f"Screened (may trade as L0/L1): {tag}." in out


def test_review_tools_run_on_local_results(write_config, tmp_path, capsys):
    results = tmp_path / "results"
    path = write_config(
        {"risk": COMPLETE_RISK, "sim": COMPLETE_SIM, "agent": {"results_dir": str(results)}}
    )
    assert main(["review", "tools", "--config", str(path)]) == 0
    names = {t["name"] for t in json.loads(capsys.readouterr().out)["result"]}
    assert {"decisions", "review", "add_lesson", "record"} <= names
    # No session journal yet: a clear error, not a crash.
    assert main(["review", "decisions", "--config", str(path)]) == 1
    assert "no session journal" in json.loads(capsys.readouterr().out)["error"]
    assert main(["review", "lessons", "not json", "--config", str(path)]) == 2
