import subprocess
from datetime import date
from pathlib import Path

import pytest

from perps_agent.app.results import (
    PublishError,
    fetch_results,
    local_days,
    publish_results,
)
from perps_agent.config.models import ResultsConfig

CONFIG = ResultsConfig()
DAY = date(2026, 10, 20)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "remote", "add", "origin", str(remote))
    return work


def _day(repo: Path, day: date = DAY) -> Path:
    day_dir = repo / "results" / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "journal.jsonl").write_text("{}\n")
    (day_dir / "report.md").write_text("# report\n")
    return day_dir


def _branch_files(repo: Path) -> list[str]:
    remote = repo.parent / "remote.git"
    return _git(remote, "ls-tree", "-r", "--name-only", "paper-results").splitlines()


def _publish(repo: Path, days, **kwargs):
    return publish_results(repo / "results", days, config=CONFIG, repo=repo, **kwargs)


def test_first_publish_creates_the_branch(repo):
    _day(repo)
    result = _publish(repo, [DAY])
    assert result.commit is not None and result.attempts == 1
    assert _branch_files(repo) == [
        "results/2026-10-20/journal.jsonl",
        "results/2026-10-20/report.md",
    ]
    remote = repo.parent / "remote.git"
    assert _git(remote, "log", "-1", "--format=%an %s", "paper-results") == (
        "perps-agent Paper results 2026-10-20"
    )
    # The checkout is untouched: no commit on the current branch, nothing staged.
    assert _git(repo, "ls-files") == ""
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True).returncode


def test_publishing_again_is_a_no_op_and_changes_add_a_commit(repo):
    _day(repo)
    first = _publish(repo, [DAY]).commit
    assert _publish(repo, [DAY]).commit is None
    (repo / "results" / "2026-10-20" / "report.md").write_text("# updated\n")
    (repo / "results" / "2026-10-20" / "journal.jsonl").unlink()
    second = _publish(repo, [DAY]).commit
    remote = repo.parent / "remote.git"
    assert _git(remote, "rev-parse", "paper-results^") == first
    assert _branch_files(repo) == ["results/2026-10-20/report.md"]
    assert second is not None


def test_other_days_on_the_branch_are_kept(repo):
    _day(repo)
    _publish(repo, [DAY])
    other = date(2026, 10, 21)
    _day(repo, other)
    _publish(repo, local_days(repo / "results"))
    assert len(_branch_files(repo)) == 4


def test_only_result_files_are_published_and_secrets_are_redacted(repo):
    day_dir = _day(repo)
    (day_dir / "ledger.jsonl").write_text('{"reason": "key s3cr3t-value"}\n')
    (day_dir / "notes.bin").write_bytes(b"\x00")
    _publish(repo, [DAY], secrets=["s3cr3t-value"])
    assert "results/2026-10-20/notes.bin" not in _branch_files(repo)
    remote = repo.parent / "remote.git"
    text = _git(remote, "show", "paper-results:results/2026-10-20/ledger.jsonl")
    assert "s3cr3t-value" not in text and "[REDACTED]" in text


def test_a_failing_push_is_retried_then_raises(repo, tmp_path):
    _day(repo)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    sleeps: list[float] = []
    with pytest.raises(PublishError, match="after 4 attempts"):
        _publish(repo, [DAY], sleep=sleeps.append)
    assert sleeps == [2.0, 4.0, 8.0]


def test_a_missing_day_is_an_error(repo):
    with pytest.raises(PublishError, match="does not exist"):
        _publish(repo, [DAY])


def test_fetch_results_restores_missing_days(repo, tmp_path):
    _day(repo)
    _day(repo, date(2026, 10, 21))
    _publish(repo, local_days(repo / "results"))
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", str(tmp_path / "remote.git"))
    (clone / "results" / "2026-10-21").mkdir(parents=True)
    fetched = fetch_results(clone / "results", config=CONFIG, repo=clone, days=10)
    assert fetched == [DAY]
    assert (clone / "results" / "2026-10-20" / "report.md").read_text() == "# report\n"
    assert not (clone / "results" / "2026-10-21" / "report.md").exists()


def test_fetch_without_a_branch_finds_nothing(repo):
    assert fetch_results(repo / "results", config=CONFIG, repo=repo) == []


def test_local_days_ignores_other_entries(tmp_path):
    (tmp_path / "2026-10-20").mkdir()
    (tmp_path / "notes").mkdir()
    (tmp_path / "2026-10-19").write_text("")
    assert local_days(tmp_path) == [DAY]
    assert local_days(tmp_path / "missing") == []


def test_results_branch_cannot_be_main():
    with pytest.raises(ValueError):
        ResultsConfig(branch="main")
