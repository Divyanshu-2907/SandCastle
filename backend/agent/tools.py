"""The four tools the agent gets, plus the dispatcher that runs them.

All I/O goes through a `WorkspaceBackend`, so the same tools work against a local
directory (CLI, tests) or a sandbox container (the worker) without knowing which.
"""

from typing import Any

from agent.backends import FileNotFound, WorkspaceBackend, WorkspaceError
from agent.executors import DEFAULT_COMMAND_TIMEOUT, truncate
from agent.guardrails import CommandRejected, PathOutsideWorkspace, check_command
from agent.schemas import ToolResult

MAX_FILE_CHARS = 60_000
MAX_LISTING_ENTRIES = 400

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file from the repository. Paths are relative to the "
            "repository root. Returns the file contents with line numbers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root, e.g. src/main.py",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "write_file",
        "description": (
            "Write a UTF-8 text file, creating parent directories as needed and "
            "overwriting any existing file. Read a file before rewriting it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repository root."},
                "content": {"type": "string", "description": "Full new contents of the file."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "list_files",
        "description": (
            "List files and directories. Pass '.' for the repository root. "
            "Version-control and dependency directories are omitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory relative to the repository root; '.' is the root.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List nested files as well. Defaults to false.",
                },
            },
            "required": ["directory"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command from the repository root and return its exit code, "
            "stdout and stderr. Use it for tests, builds and git. Commands that touch "
            "the network, escalate privileges or delete recursively are rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Seconds before the command is killed. "
                        f"Defaults to {DEFAULT_COMMAND_TIMEOUT}."
                    ),
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = frozenset(str(t["name"]) for t in TOOL_DEFINITIONS)


class Toolbox:
    """Executes tool calls against one workspace."""

    def __init__(self, backend: WorkspaceBackend) -> None:
        self.backend = backend

    def dispatch(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        handler = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "list_files": self._list_files,
            "run_command": self._run_command,
        }.get(name)

        if handler is None:
            available = ", ".join(sorted(TOOL_NAMES))
            return ToolResult(
                text=f"Unknown tool {name!r}. Available tools: {available}.",
                payload={"error": "unknown_tool", "tool": name},
                is_error=True,
            )

        try:
            return handler(tool_input)
        except (PathOutsideWorkspace, CommandRejected) as exc:
            # The only place `blocked` is set: these two exceptions are the
            # guardrails refusing, not a tool failing.
            return ToolResult(
                text=f"Rejected: {exc}",
                payload={"error": str(exc)},
                is_error=True,
                blocked=True,
            )
        except WorkspaceError as exc:
            detail = str(exc) or type(exc).__name__
            return ToolResult(text=detail, payload={"error": detail}, is_error=True)
        except Exception as exc:  # noqa: BLE001 - reported to the model, never kills the run
            detail = f"{type(exc).__name__}: {exc}"
            return ToolResult(text=detail, payload={"error": detail}, is_error=True)

    # -- handlers ---------------------------------------------------------

    def _read_file(self, tool_input: dict[str, Any]) -> ToolResult:
        rel = tool_input["path"]
        try:
            raw = self.backend.read_file(rel)
        except FileNotFound:
            return ToolResult(
                text=f"No such file: {rel}",
                payload={"error": "not_found", "path": rel},
                is_error=True,
            )

        body = truncate(raw, MAX_FILE_CHARS)
        numbered = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(body.splitlines(), 1))
        return ToolResult(
            text=numbered or "(empty file)",
            payload={"path": rel, "bytes": len(raw), "lines": len(raw.splitlines())},
        )

    def _write_file(self, tool_input: dict[str, Any]) -> ToolResult:
        rel = tool_input["path"]
        content = tool_input["content"]
        existed = self.backend.write_file(rel, content)

        verb = "Updated" if existed else "Created"
        return ToolResult(
            text=f"{verb} {rel} ({len(content)} bytes).",
            payload={"path": rel, "bytes": len(content), "created": not existed},
        )

    def _list_files(self, tool_input: dict[str, Any]) -> ToolResult:
        rel = tool_input.get("directory") or "."
        recursive = bool(tool_input.get("recursive", False))
        try:
            found = self.backend.list_dir(rel, recursive=recursive)
        except FileNotFound:
            return ToolResult(
                text=f"No such directory: {rel}",
                payload={"error": "not_found", "directory": rel},
                is_error=True,
            )

        truncated = len(found) > MAX_LISTING_ENTRIES
        entries = found[:MAX_LISTING_ENTRIES]
        listing = list(entries)
        if truncated:
            listing.append(f"... [truncated at {MAX_LISTING_ENTRIES} entries]")

        return ToolResult(
            text="\n".join(listing) or "(empty directory)",
            payload={"directory": rel, "count": len(entries), "truncated": truncated},
        )

    def _run_command(self, tool_input: dict[str, Any]) -> ToolResult:
        command = tool_input["command"]
        check_command(command)  # raises CommandRejected, caught by dispatch()

        timeout = int(tool_input.get("timeout") or DEFAULT_COMMAND_TIMEOUT)
        outcome = self.backend.run(command, timeout=timeout)

        parts = [f"exit_code: {outcome.exit_code}"]
        if outcome.timed_out:
            parts.append(f"(killed after {timeout}s timeout)")
        if outcome.stdout:
            parts.append(f"stdout:\n{outcome.stdout}")
        if outcome.stderr:
            parts.append(f"stderr:\n{outcome.stderr}")
        if not outcome.stdout and not outcome.stderr:
            parts.append("(no output)")

        return ToolResult(
            text="\n".join(parts),
            payload={
                "command": command,
                "exit_code": outcome.exit_code,
                "timed_out": outcome.timed_out,
                "stdout": outcome.stdout,
                "stderr": outcome.stderr,
            },
            is_error=not outcome.succeeded,
        )
