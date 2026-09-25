"""Where the agent's workspace physically lives.

Two implementations behind one protocol:

- `LocalBackend` -- a directory on this machine. Used by the `run_agent.py` CLI
  and by tests. No isolation beyond the guardrail blocklist.
- `DockerBackend` -- a directory inside a locked-down sibling container. What the
  worker uses, and the actual containment boundary.

Every file operation is routed through the backend rather than through `pathlib`
because the worker is itself a container: it cannot bind-mount its own
filesystem into a sibling container, so the untrusted workspace has to live
inside the sandbox and be reached over the Docker API.
"""

import io
import logging
import shlex
import tarfile
from pathlib import Path
from typing import Any, Protocol

from agent.executors import DEFAULT_COMMAND_TIMEOUT, CommandOutcome, truncate
from agent.guardrails import resolve_in_workspace

logger = logging.getLogger(__name__)

WORKSPACE_DIR = "/workspace"

# Never worth showing the model, and they would dominate any listing.
IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


class WorkspaceError(Exception):
    """A workspace operation failed in a way the model should hear about."""


class FileNotFound(WorkspaceError):
    pass


class WorkspaceBackend(Protocol):
    """The filesystem + shell the agent acts on."""

    def run(self, command: str, timeout: int = DEFAULT_COMMAND_TIMEOUT) -> CommandOutcome: ...

    def read_file(self, path: str) -> str: ...

    def write_file(self, path: str, content: str) -> bool:
        """Write the file. Returns True if it already existed."""
        ...

    def list_dir(self, path: str, recursive: bool = False) -> list[str]: ...


# -- local ----------------------------------------------------------------


class LocalBackend:
    """The workspace is a plain directory on this machine.

    Path containment is enforced in Python (`resolve_in_workspace`), which is a
    guardrail rather than a boundary -- anything the agent runs via `run` is a
    host process with this user's privileges. Fine for the CLI against a repo you
    trust; `DockerBackend` is what the platform actually runs.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def run(self, command: str, timeout: int = DEFAULT_COMMAND_TIMEOUT) -> CommandOutcome:
        import subprocess

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            return CommandOutcome(
                command=command,
                exit_code=124,
                stdout=truncate(_as_text(exc.stdout)),
                stderr=truncate(_as_text(exc.stderr)),
                timed_out=True,
            )
        return CommandOutcome(
            command=command,
            exit_code=completed.returncode,
            stdout=truncate(completed.stdout),
            stderr=truncate(completed.stderr),
        )

    def read_file(self, path: str) -> str:
        target = resolve_in_workspace(self.workspace, path)
        if not target.is_file():
            raise FileNotFound(path)
        return target.read_text(encoding="utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> bool:
        target = resolve_in_workspace(self.workspace, path)
        existed = target.is_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        return existed

    def list_dir(self, path: str, recursive: bool = False) -> list[str]:
        directory = resolve_in_workspace(self.workspace, path)
        if not directory.is_dir():
            raise FileNotFound(path)

        children = sorted(directory.rglob("*") if recursive else directory.iterdir())
        entries: list[str] = []
        for child in children:
            relative = child.relative_to(self.workspace)
            if any(part in IGNORED_DIRS for part in relative.parts):
                continue
            name = relative.as_posix()
            entries.append(f"{name}/" if child.is_dir() else name)
        return entries


# -- docker ---------------------------------------------------------------


class DockerBackend:
    """The workspace lives inside a sandbox container, reached over the Docker API.

    `container` is a `docker.models.containers.Container` that is already running
    with the workspace volume mounted at `WORKSPACE_DIR` and no network.
    """

    def __init__(self, container: Any, workdir: str = WORKSPACE_DIR) -> None:
        self.container = container
        self.workdir = workdir

    # -- shell ------------------------------------------------------------

    def run(self, command: str, timeout: int = DEFAULT_COMMAND_TIMEOUT) -> CommandOutcome:
        # `timeout` inside the container rather than a client-side deadline:
        # Docker has no way to cancel a running exec, so a client-side timeout
        # would return while the process kept burning CPU in the sandbox.
        wrapped = f"timeout --signal=KILL {int(timeout)}s sh -c {shlex.quote(command)}"
        exit_code, stdout, stderr = self._exec(wrapped)

        # 137 = SIGKILL, which is what `timeout --signal=KILL` produces.
        timed_out = exit_code in (124, 137)
        return CommandOutcome(
            command=command,
            exit_code=exit_code,
            stdout=truncate(stdout),
            stderr=truncate(stderr),
            timed_out=timed_out,
        )

    def _exec(self, command: str, workdir: str | None = None) -> tuple[int, str, str]:
        result = self.container.exec_run(
            ["sh", "-c", command],
            workdir=workdir or self.workdir,
            demux=True,
            user="agent",
        )
        stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
        return result.exit_code, _as_text(stdout), _as_text(stderr)

    # -- files ------------------------------------------------------------

    def read_file(self, path: str) -> str:
        safe = _safe_relative(path)
        # get_archive rather than `cat`: it does not go through a shell, so file
        # names and contents cannot be reinterpreted as shell syntax.
        try:
            stream, _ = self.container.get_archive(f"{self.workdir}/{safe}")
        except Exception as exc:  # noqa: BLE001 - docker raises NotFound for both
            raise FileNotFound(path) from exc

        buffer = io.BytesIO(b"".join(stream))
        with tarfile.open(fileobj=buffer) as archive:
            member = next((m for m in archive.getmembers() if m.isfile()), None)
            if member is None:
                raise FileNotFound(path)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise FileNotFound(path)
            return extracted.read().decode("utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> bool:
        safe = _safe_relative(path)
        existed = self._exists(safe, kind="f")

        parent = str(Path(safe).parent.as_posix())
        if parent not in (".", ""):
            self._exec(f"mkdir -p {shlex.quote(parent)}")

        payload = content.encode("utf-8")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo(name=Path(safe).name)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = info.gid = 10001  # the sandbox image's `agent` user
            archive.addfile(info, io.BytesIO(payload))

        destination = f"{self.workdir}/{parent}" if parent not in (".", "") else self.workdir
        if not self.container.put_archive(destination, buffer.getvalue()):
            raise WorkspaceError(f"could not write {path}")
        return existed

    def list_dir(self, path: str, recursive: bool = False) -> list[str]:
        safe = _safe_relative(path)
        target = f"{self.workdir}/{safe}" if safe != "." else self.workdir

        if not self._exists(safe, kind="d"):
            raise FileNotFound(path)

        depth = "" if recursive else "-maxdepth 1"
        # -printf keeps directories distinguishable without a second call.
        exit_code, stdout, stderr = self._exec(
            f"find {shlex.quote(target)} {depth} -mindepth 1 -printf '%y %P\\n' | sort -k2"
        )
        if exit_code != 0:
            raise WorkspaceError(stderr.strip() or f"could not list {path}")

        entries: list[str] = []
        prefix = "" if safe == "." else f"{safe}/"
        for line in stdout.splitlines():
            if not line.strip():
                continue
            kind, _, name = line.partition(" ")
            if not name or any(part in IGNORED_DIRS for part in Path(name).parts):
                continue
            entries.append(f"{prefix}{name}/" if kind == "d" else f"{prefix}{name}")
        return entries

    def _exists(self, relative: str, kind: str) -> bool:
        target = f"{self.workdir}/{relative}" if relative != "." else self.workdir
        exit_code, _, _ = self._exec(f"test -{kind} {shlex.quote(target)}")
        return exit_code == 0


def _safe_relative(path: str) -> str:
    """Reject traversal before it ever reaches the container.

    The container boundary is the real protection -- an escape only reaches the
    sandbox's own filesystem -- but keeping the check means the model gets a
    clear rejection instead of a confusing `find` error.
    """
    from pathlib import PurePosixPath

    candidate = PurePosixPath((path or ".").replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        from agent.guardrails import PathOutsideWorkspace

        raise PathOutsideWorkspace(f"path escapes the workspace: {path}")
    return candidate.as_posix() or "."


def _as_text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw
