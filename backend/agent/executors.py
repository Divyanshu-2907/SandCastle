"""Shared vocabulary for command results.

The execution strategies themselves live in `backends.py` (`LocalBackend` for a
host directory, `DockerBackend` for a sandbox container).
"""

from dataclasses import dataclass

DEFAULT_COMMAND_TIMEOUT = 60
# Tool output is fed straight back into the context window; unbounded build logs
# would blow the budget in one step.
MAX_OUTPUT_CHARS = 8_000


@dataclass(slots=True)
class CommandOutcome:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{dropped} characters truncated] ...\n\n{tail}"
