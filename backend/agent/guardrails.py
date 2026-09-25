"""First-line guardrails for agent-issued shell commands.

This is a blocklist, and blocklists leak by construction -- a determined agent
can evade any of these patterns. It exists to catch the realistic failure mode
(a confused model running something destructive) while Phase 5 builds the actual
containment boundary. Treat it as defence in depth, never as the boundary.
"""

import re
import shlex
from pathlib import Path, PurePosixPath

# (compiled pattern, human-readable reason)
_BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", re.I), "recursive/forced delete"),
    (re.compile(r"\brm\s+.*(/|\s)\*", re.I), "wildcard delete"),
    (re.compile(r"\b(sudo|su|doas)\b", re.I), "privilege escalation"),
    (re.compile(r"\b(mkfs|fdisk|parted)\b", re.I), "disk formatting"),
    (re.compile(r"\bdd\s+.*\bof=/dev/", re.I), "raw device write"),
    (re.compile(r">\s*/dev/(sd|nvme|hd)", re.I), "raw device write"),
    (re.compile(r"\bchmod\s+(-[a-zA-Z]+\s+)*777\b", re.I), "world-writable permissions"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b", re.I), "host power control"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;?\s*:", re.S), "fork bomb"),
    (re.compile(r"\b(curl|wget)\b", re.I), "outbound network fetch"),
    (re.compile(r"\b(nc|ncat|netcat|telnet|ssh|scp|sftp|rsync)\b", re.I), "network/remote shell"),
    (re.compile(r"\bdocker\b|\bkubectl\b", re.I), "container escape surface"),
    (re.compile(r"/etc/(passwd|shadow|sudoers)", re.I), "system credential file"),
    (re.compile(r"~?/\.(ssh|aws|config/gcloud)\b", re.I), "credential directory"),
    (re.compile(r"\b(history|env|printenv)\b\s*\|", re.I), "environment exfiltration"),
    (re.compile(r"\bcrontab\b|\bat\s+now\b", re.I), "scheduled execution"),
    (re.compile(r"\bgit\s+push\b", re.I), "pushing to a remote"),
    (re.compile(r"\bpip\s+install\b.*\b(https?|git\+)", re.I), "install from arbitrary URL"),
    (re.compile(r"\|\s*(ba)?sh\b", re.I), "piping downloaded content to a shell"),
]

# Commands that reach the network legitimately. `git clone`/`fetch`/`pull` are
# how a repo gets set up, so they are exempted from the network blocks above.
_NETWORK_ALLOWED = re.compile(r"^\s*git\s+(clone|fetch|pull|remote|ls-remote)\b", re.I)


class CommandRejected(Exception):
    """Raised when a command trips a guardrail."""

    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"command rejected ({reason}): {command}")


def check_command(command: str) -> None:
    """Raise `CommandRejected` if `command` matches a blocked pattern."""
    stripped = command.strip()
    if not stripped:
        raise CommandRejected(command, "empty command")

    network_exempt = bool(_NETWORK_ALLOWED.match(stripped))
    for pattern, reason in _BLOCKED_PATTERNS:
        if not pattern.search(stripped):
            continue
        if network_exempt and reason in {"outbound network fetch", "network/remote shell"}:
            continue
        raise CommandRejected(command, reason)


def is_allowed(command: str) -> bool:
    try:
        check_command(command)
    except CommandRejected:
        return False
    return True


class PathOutsideWorkspace(Exception):
    """Raised when a file tool is pointed outside the run's workspace."""


def resolve_in_workspace(workspace: Path, relative_path: str) -> Path:
    """Resolve `relative_path` against `workspace`, refusing to escape it.

    Rejects absolute paths, `..` traversal, and symlinks that point outside --
    `Path.resolve()` follows links, so the containment check sees the real target.
    """
    candidate = PurePosixPath(relative_path.replace("\\", "/"))
    if candidate.is_absolute():
        raise PathOutsideWorkspace(f"absolute paths are not allowed: {relative_path}")

    workspace_root = workspace.resolve()
    resolved = (workspace_root / Path(*candidate.parts)).resolve()
    if resolved != workspace_root and workspace_root not in resolved.parents:
        raise PathOutsideWorkspace(f"path escapes the workspace: {relative_path}")
    return resolved


def shell_words(command: str) -> list[str]:
    """Best-effort tokenisation, used only for logging."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()
