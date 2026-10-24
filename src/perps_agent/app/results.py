"""The results branch (ADR-0006): publish ``results/<date>/`` and fetch past days back.

:func:`publish_results` commits day directories to ``results.branch`` (by
default ``paper-results``) as ``results/<date>/...`` and pushes it. It uses
git plumbing with a temporary index, so the checkout, its index and the
current branch are never touched, and the branch is never merged into
``main``. A rejected or failed push is retried (fetch, rebuild on the new tip,
push) with 2, 4, 8 ... seconds between attempts. Publishing every local day is
idempotent: a day already on the branch adds nothing, so the next session
retries whatever an earlier one failed to publish.

The backtest's Gate 1 records (``gate1/``) and reports (``backtests/``) and the
lessons notebook (``lessons/``) are published with them, file by file: a file
on the branch is updated, never removed, so a session that has not fetched
them cannot delete them.

Only ``.jsonl``, ``.json`` and ``.md`` files are published, and known secret
values are replaced in them first (overview §7).

:func:`fetch_results` copies recent days from the branch into the local
results directory (for the briefing's recent activity), never overwriting a
day that exists locally, the Gate 1 records that are missing locally or
newer on the branch (for the session's screened playbooks), and the lessons
notebook when the branch's copy has more events.
"""

import io
import json
import logging
import os
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from perps_agent.backtest import BACKTESTS_DIR, GATE1_DIR
from perps_agent.config.models import ResultsConfig
from perps_agent.learning.lessons import LESSONS_DIR, LESSONS_FILE
from perps_agent.log import REDACTED

log = logging.getLogger(__name__)

# Path of the day directories on the branch.
BRANCH_DIR = "results"
PUBLISHED_SUFFIXES = (".jsonl", ".json", ".md")
# Directories next to the days, published file by file (see the module docstring).
SHARED_DIRS = (GATE1_DIR, BACKTESTS_DIR, LESSONS_DIR)
_EMPTY_TREE_ARGS = ("read-tree", "--empty")
# Results commits are made by the bot, whoever runs the session.
_IDENTITY = {
    "GIT_AUTHOR_NAME": "perps-agent",
    "GIT_AUTHOR_EMAIL": "perps-agent@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "perps-agent",
    "GIT_COMMITTER_EMAIL": "perps-agent@users.noreply.github.com",
}


class PublishError(Exception):
    """The results could not be published (or fetched)."""


@dataclass(frozen=True, slots=True)
class PublishResult:
    days: tuple[date, ...]
    # ``None``: nothing changed, the branch already had these files.
    commit: str | None
    attempts: int


def local_days(results_dir: Path) -> list[date]:
    """Day directories (``YYYY-MM-DD``) under the local results directory, oldest first."""
    days = []
    if results_dir.is_dir():
        for child in results_dir.iterdir():
            try:
                day = date.fromisoformat(child.name)
            except ValueError:
                continue
            if child.is_dir():
                days.append(day)
    return sorted(days)


def publish_results(
    results_dir: Path,
    days: Sequence[date],
    *,
    config: ResultsConfig,
    repo: Path = Path("."),
    secrets: Iterable[str] = (),
    sleep: Callable[[float], None] = time.sleep,
) -> PublishResult:
    """Commit ``days`` and the shared directories from ``results_dir`` to the branch, and push."""
    days = tuple(sorted(set(days)))
    files = {day: _day_files(results_dir / day.isoformat()) for day in days}
    shared = _shared_files(results_dir)
    if not days and not shared:
        return PublishResult(days=(), commit=None, attempts=0)
    redact = sorted({s for s in secrets if s}, key=len, reverse=True)
    git = _Git(repo)
    error: PublishError | None = None
    for attempt in range(1, config.push_attempts + 1):
        try:
            parent = git.fetch_branch(config.remote, config.branch)
            commit = git.commit_days(parent, files, redact, _message(days), shared=shared)
            if commit is None:
                return PublishResult(days=days, commit=None, attempts=attempt)
            git.run("push", config.remote, f"{commit}:refs/heads/{config.branch}")
            log.info(
                "results published",
                extra={"branch": config.branch, "commit": commit, "days": [str(d) for d in days]},
            )
            return PublishResult(days=days, commit=commit, attempts=attempt)
        except PublishError as failure:
            error = failure
            log.warning("publish attempt failed", extra={"attempt": attempt, "error": str(failure)})
            if attempt < config.push_attempts:
                sleep(2.0**attempt)
    assert error is not None
    raise PublishError(f"could not publish after {config.push_attempts} attempts: {error}")


def fetch_results(
    results_dir: Path, *, config: ResultsConfig, repo: Path = Path("."), days: int = 10
) -> list[date]:
    """Copy the newest ``days`` days from the branch that are missing locally.

    Gate 1 records missing locally, or newer on the branch, are copied too.
    """
    git = _Git(repo)
    tip = git.fetch_branch(config.remote, config.branch)
    if tip is None:
        return []
    _fetch_gate1(git, tip, results_dir)
    _fetch_lessons(git, tip, results_dir)
    listing = git.run("ls-tree", "--name-only", f"{tip}:{BRANCH_DIR}").stdout.decode().split()
    remote_days = []
    for name in listing:
        try:
            remote_days.append(date.fromisoformat(name))
        except ValueError:
            continue
    wanted = [d for d in sorted(remote_days)[-days:] if not (results_dir / d.isoformat()).exists()]
    if not wanted:
        return []
    paths = [f"{BRANCH_DIR}/{d.isoformat()}" for d in wanted]
    archive = git.run("archive", "--format=tar", tip, *paths).stdout
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp, filter="data")
        results_dir.mkdir(parents=True, exist_ok=True)
        for day in wanted:
            (Path(tmp) / BRANCH_DIR / day.isoformat()).rename(results_dir / day.isoformat())
    return wanted


def _fetch_gate1(git: "_Git", tip: str, results_dir: Path) -> None:
    prefix = f"{BRANCH_DIR}/{GATE1_DIR}"
    listing = git.run("ls-tree", "--name-only", f"{tip}:{BRANCH_DIR}").stdout.decode().split()
    if GATE1_DIR not in listing:
        return
    names = git.run("ls-tree", "--name-only", f"{tip}:{prefix}").stdout.decode().split()
    local_dir = results_dir / GATE1_DIR
    for name in names:
        if not name.endswith(".json"):
            continue
        remote = git.run("show", f"{tip}:{prefix}/{name}").stdout
        local = local_dir / name
        if local.exists() and _run_at(local.read_bytes()) >= _run_at(remote):
            continue
        local_dir.mkdir(parents=True, exist_ok=True)
        local.write_bytes(remote)


def _fetch_lessons(git: "_Git", tip: str, results_dir: Path) -> None:
    """The notebook is append-only: take the branch's copy when it has more events."""
    listing = git.run("ls-tree", "--name-only", f"{tip}:{BRANCH_DIR}").stdout.decode().split()
    if LESSONS_DIR not in listing:
        return
    names = git.run("ls-tree", "--name-only", f"{tip}:{BRANCH_DIR}/{LESSONS_DIR}").stdout
    if LESSONS_FILE not in names.decode().split():
        return
    remote = git.run("show", f"{tip}:{BRANCH_DIR}/{LESSONS_DIR}/{LESSONS_FILE}").stdout
    local = results_dir / LESSONS_DIR / LESSONS_FILE
    if local.exists() and len(local.read_bytes().splitlines()) >= len(remote.splitlines()):
        return
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(remote)


def _run_at(data: bytes) -> str:
    try:
        record = json.loads(data)
    except ValueError:
        return ""
    value = record.get("run_at") if isinstance(record, dict) else None
    return value if isinstance(value, str) else ""


def _shared_files(results_dir: Path) -> list[Path]:
    return sorted(
        p
        for name in SHARED_DIRS
        if (results_dir / name).is_dir()
        for p in (results_dir / name).rglob("*")
        if p.is_file() and p.suffix in PUBLISHED_SUFFIXES
    )


def _day_files(day_dir: Path) -> list[Path]:
    if not day_dir.is_dir():
        raise PublishError(f"{day_dir} does not exist")
    return sorted(p for p in day_dir.rglob("*") if p.is_file() and p.suffix in PUBLISHED_SUFFIXES)


def _message(days: Sequence[date]) -> str:
    if not days:
        return "Backtest results"
    if len(days) == 1:
        return f"Paper results {days[0].isoformat()}"
    return f"Paper results {days[0].isoformat()} to {days[-1].isoformat()} ({len(days)} days)"


class _Git:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def run(
        self, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self._repo,
                env={**os.environ, **(env or {})},
                input=stdin,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishError(f"git {args[0]}: {error}") from error
        if result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip().splitlines()
            raise PublishError(f"git {args[0]} failed: {message[-1] if message else '?'}")
        return result

    def fetch_branch(self, remote: str, branch: str) -> str | None:
        """The branch tip on the remote, fetched locally; ``None`` if the branch does not exist."""
        heads = self.run("ls-remote", "--heads", remote, f"refs/heads/{branch}").stdout.decode()
        if not heads.strip():
            return None
        tip = heads.split()[0]
        self.run("fetch", "--no-tags", remote, f"refs/heads/{branch}")
        return tip

    def commit_days(
        self,
        parent: str | None,
        files: dict[date, list[Path]],
        secrets: Sequence[str],
        message: str,
        *,
        shared: Sequence[Path] = (),
    ) -> str | None:
        """A commit on ``parent`` with each day's directory replaced; ``None`` if unchanged.

        ``shared`` files are added or updated at their path under the results
        directory, without removing anything.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.run(*(("read-tree", parent) if parent else _EMPTY_TREE_ARGS), env=env)
            for day, paths in files.items():
                prefix = PurePosixPath(BRANCH_DIR, day.isoformat())
                old = self.run("ls-files", "-z", "--", str(prefix), env=env).stdout
                if old:
                    self.run("update-index", "--force-remove", "-z", "--stdin", env=env, stdin=old)
                for path in paths:
                    relative = path.relative_to(_day_root(path, day))
                    self._add(path, prefix / relative.as_posix(), secrets, env)
            for path in shared:
                relative = path.relative_to(_shared_root(path))
                self._add(path, PurePosixPath(BRANCH_DIR, relative.as_posix()), secrets, env)
            tree = self.run("write-tree", env=env).stdout.decode().strip()
        if parent is not None:
            parent_tree = self.run("rev-parse", f"{parent}^{{tree}}").stdout.decode().strip()
            if parent_tree == tree:
                return None
        args = ["commit-tree", tree, "-m", message]
        if parent is not None:
            args += ["-p", parent]
        return self.run(*args, env=_IDENTITY).stdout.decode().strip()

    def _add(
        self, path: Path, target: PurePosixPath, secrets: Sequence[str], env: dict[str, str]
    ) -> None:
        blob = (
            self.run("hash-object", "-w", "--stdin", stdin=_redacted(path, secrets))
            .stdout.decode()
            .strip()
        )
        self.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{target}", env=env)


def _shared_root(path: Path) -> Path:
    """The results directory holding ``path`` (the parent of its shared directory)."""
    for parent in path.parents:
        if parent.name in SHARED_DIRS:
            return parent.parent
    raise PublishError(f"{path} is not inside {' or '.join(SHARED_DIRS)}")


def _day_root(path: Path, day: date) -> Path:
    for parent in path.parents:
        if parent.name == day.isoformat():
            return parent
    raise PublishError(f"{path} is not inside a {day.isoformat()} directory")


def _redacted(path: Path, secrets: Sequence[str]) -> bytes:
    data = path.read_bytes()
    for secret in secrets:
        data = data.replace(secret.encode(), REDACTED.encode())
    return data
