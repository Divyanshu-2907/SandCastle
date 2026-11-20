"""Transport objects shared by the agent loop, the CLI, and the Celery worker.

`AgentStep` deliberately mirrors the `steps` table so persisting a step is a
field-for-field copy rather than a translation layer.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

StepTypeLiteral = Literal["thought", "action", "observation"]


@dataclass(slots=True)
class AgentStep:
    step_number: int
    type: StepTypeLiteral
    content: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ToolResult:
    """What a tool hands back: `payload` is logged, `text` is what the model sees."""

    text: str
    payload: dict[str, Any]
    is_error: bool = False
    # A guardrail refused the call, rather than the tool trying and failing.
    # Both read as errors to the model, but only this one is the sandbox working
    # as designed, and traces and scores have to tell them apart. Carried as a
    # field because the alternative is matching on the "Rejected: " prefix of
    # `text`, which would break the first time that wording changes.
    blocked: bool = False


@dataclass(slots=True)
class AgentResult:
    steps: list[AgentStep] = field(default_factory=list)
    final_message: str | None = None
    diff: str = ""
    stop_reason: str = "end_turn"
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    @property
    def step_count(self) -> int:
        return len(self.steps)
