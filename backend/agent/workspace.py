"""Cloning a repo into a scratch workspace and reading the agent's changes back out."""

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CLONE_TIMEOUT = 180


class WorkspaceError(Exception):
    """Raised when a repo cannot be cloned or inspected."""


@dataclass(slots=True)
class FileDiff:
    file_path: str
    diff_text: str


def _git(workspace: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
    )


def clone_repo(repo_url: str, destination: Path, depth: int = 1) -> Path:
    """Shallow-clone `repo_url` into `destination`.

    Shallow by default: runs are one-shot edits, so the history is dead weight.
    """
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", "--depth", str(depth), "--quiet", repo_url, str(destination)],
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT,
        errors="replace",
    )
    if result.returncode != 0:
        raise WorkspaceError(f"git clone failed: {result.stderr.strip() or result.stdout.strip()}")

    # Detached commits during a run need an author; the clone has no user config.
    _git(destination, "config", "user.email", "agent@sandcastle.local")
    _git(destination, "config", "user.name", "Sandcastle Agent")
    return destination


@contextmanager
def temporary_clone(repo_url: str, depth: int = 1) -> Iterator[Path]:
    """Clone into a temp dir, yield it, and always clean up."""
    tmp = Path(tempfile.mkdtemp(prefix="sandcastle-"))
    workspace = tmp / "repo"
    try:
        yield clone_repo(repo_url, workspace, depth=depth)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def collect_diff(workspace: Path) -> str:
    """Unified diff of everything the agent changed, new files included.

    `git add -N` registers untracked files with the index so `git diff` reports
    them; without it a brand-new file produces an empty diff.
    """
    _git(workspace, "add", "--intent-to-add", "--all")
    result = _git(workspace, "diff", "--no-color")
    if result.returncode != 0:
        raise WorkspaceError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def changed_files(workspace: Path) -> list[str]:
    _git(workspace, "add", "--intent-to-add", "--all")
    result = _git(workspace, "diff", "--name-only")
    return [line for line in result.stdout.splitlines() if line.strip()]


def collect_file_diffs(workspace: Path) -> list[FileDiff]:
    """Per-file diffs, one row per changed file for the `diffs` table."""
    diffs: list[FileDiff] = []
    for path in changed_files(workspace):
        result = _git(workspace, "diff", "--no-color", "--", path)
        text = result.stdout
        if text.strip():
            diffs.append(FileDiff(file_path=path, diff_text=text))
    return diffs


# -- backend-based variants -----------------------------------------------
#
# The sandbox workspace lives inside a container, so it cannot be reached with
# subprocess. These run the same git commands through whatever backend the run
# is using, which keeps one code path for local and sandboxed runs alike.


def _git_via(backend, args: str, timeout: int = 60):
    return backend.run(f"git {args}", timeout=timeout)


def prepare_git_identity(backend) -> None:
    """A clone has no committer configured; `git add -N` needs one."""
    _git_via(backend, "config user.email agent@sandcastle.local")
    _git_via(backend, "config user.name 'Sandcastle Agent'")


def collect_diff_via(backend) -> str:
    """Unified diff of everything the agent changed, new files included."""
    # `add -N` registers untracked files with the index so `git diff` reports
    # them; without it a brand-new file produces an empty diff.
    _git_via(backend, "add --intent-to-add --all")
    outcome = _git_via(backend, "diff --no-color")
    if outcome.exit_code != 0:
        raise WorkspaceError(f"git diff failed: {outcome.stderr.strip()}")
    return outcome.stdout


def changed_files_via(backend) -> list[str]:
    _git_via(backend, "add --intent-to-add --all")
    outcome = _git_via(backend, "diff --name-only")
    return [line for line in outcome.stdout.splitlines() if line.strip()]


def collect_file_diffs_via(backend) -> list[FileDiff]:
    """Per-file diffs, one row per changed file for the `diffs` table."""
    diffs: list[FileDiff] = []
    for path in changed_files_via(backend):
        outcome = _git_via(backend, f"diff --no-color -- {path}")
        if outcome.stdout.strip():
            diffs.append(FileDiff(file_path=path, diff_text=outcome.stdout))
    return diffs
