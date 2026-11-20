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
            "repository root. Returns the file contents with line numbers. Pass "
            "offset and limit to read part of a large file instead of all of it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the repository root, e.g. src/main.py",
                },
                "offset": {
                    "type": "integer",
                    "description": "First line to return, 1-based. Defaults to the start.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many lines to return. Defaults to the whole file.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace one exact snippet in an existing file. Use this for changes to "
            "files that already exist: it sends only the snippet, where write_file "
            "has to resend the entire file. old_string must appear exactly once -- "
            "include surrounding lines to make it unique. Fails without writing if "
            "it is missing or appears more than once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repository root."},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace, including indentation.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Text to put in its place. Empty string deletes the snippet.",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a UTF-8 text file whole, creating parent directories as needed "
            "and overwriting anything already there. Use it for new files and small "
            "ones; for a change to an existing file prefer edit_file, which does not "
            "have to resend the entire contents. Read a file before rewriting it."
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
            "edit_file": self._edit_file,
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

        lines = raw.splitlines()
        total = len(lines)
        # A window into a long file, so reading one function out of a thousand
        # lines does not put the whole thousand into the context for every
        # subsequent turn. Line numbers stay absolute, so they can be quoted back.
        start = max(1, int(tool_input.get("offset") or 1))
        limit = tool_input.get("limit")
        end = start - 1 + int(limit) if limit else total
        window = lines[start - 1 : end]
        partial = start > 1 or end < total

        body = truncate("\n".join(window), MAX_FILE_CHARS)
        numbered = "\n".join(
            f"{i:>5}  {line}" for i, line in enumerate(body.splitlines(), start)
        )
        if partial and window:
            numbered += f"\n\n[lines {start}-{start + len(window) - 1} of {total}]"
        elif not window and total:
            numbered = f"(no lines in range; the file has {total})"

        return ToolResult(
            text=numbered or "(empty file)",
            payload={
                "path": rel,
                "bytes": len(raw),
                "lines": total,
                "shown": len(window),
                "offset": start,
                "partial": partial,
            },
        )

    def _edit_file(self, tool_input: dict[str, Any]) -> ToolResult:
        """Replace one exact snippet, or refuse and change nothing.

        Refusing on an ambiguous match is the point. A silent replace-the-first
        would quietly edit the wrong occurrence, and the agent cannot see the
        file afterwards unless it spends another turn reading it back.
        """
        rel = tool_input["path"]
        old = tool_input["old_string"]
        new = tool_input["new_string"]

        if not old:
            return ToolResult(
                text="old_string must not be empty. Use write_file to create a file.",
                payload={"error": "empty_old_string", "path": rel},
                is_error=True,
            )

        try:
            raw = self.backend.read_file(rel)
        except FileNotFound:
            return ToolResult(
                text=f"No such file: {rel}. Use write_file to create it.",
                payload={"error": "not_found", "path": rel},
                is_error=True,
            )

        found = raw.count(old)
        if found == 0:
            return ToolResult(
                text=(
                    f"old_string was not found in {rel}. It must match the file exactly, "
                    "including indentation and line breaks. Read the file and copy the "
                    "snippet from it."
                ),
                payload={"error": "no_match", "path": rel, "matches": 0},
                is_error=True,
            )
        if found > 1:
            return ToolResult(
                text=(
                    f"old_string matches {found} places in {rel}, so the edit is ambiguous "
                    "and nothing was changed. Include surrounding lines to make it unique."
                ),
                payload={"error": "multiple_matches", "path": rel, "matches": found},
                is_error=True,
            )

        self.backend.write_file(rel, raw.replace(old, new, 1))
        delta = len(new) - len(old)
        return ToolResult(
            text=f"Edited {rel} (1 replacement, {delta:+d} bytes).",
            payload={"path": rel, "matches": 1, "delta_bytes": delta},
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
