"""The agent loop: prompt the model, run the tool it asks for, feed the result back.

A manual loop rather than an SDK's built-in function-calling helper, because every
tool call has to clear a guardrail check first and every step has to be persisted
as it happens (Phase 3 streams them to Postgres and Redis from `on_step`).
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.backends import LocalBackend, WorkspaceBackend
from agent.llm import DEFAULT_PROVIDER, LLMClient, LLMError, ToolOutcome, build_client
from agent.schemas import AgentResult, AgentStep
from agent.tools import TOOL_DEFINITIONS, Toolbox

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_MAX_STEPS = 15

StepCallback = Callable[[AgentStep], None]

SYSTEM_PROMPT = """\
You are a coding agent working inside a git repository checkout. You have tools to \
read, list, and write files and to run shell commands from the repository root.

How to work:
- Look before you leap: list or read the relevant files before editing anything.
- `write_file` replaces the whole file, so read a file first and write it back complete.
- Make the smallest change that accomplishes the task. Match the surrounding style.
- Verify your work when the repo makes that possible: run the tests, run the linter, \
or run `git diff` to review what you changed.
- Commands that reach the network, escalate privileges, or delete recursively are \
blocked. Work within that; do not try to route around it.
- Do not commit or push. Leaving the change in the working tree is the deliverable.

When the task is done, stop calling tools and reply with a short summary of what you \
changed and why. If the task cannot be done, say so plainly and explain what blocked you.\
"""

USER_PROMPT = """\
Repository: {repo_url}
Working directory: the repository root (all tool paths are relative to it)

Task:
{description}
"""


class AgentLoop:
    """Runs one agent session against one workspace.

    `max_steps` caps model turns, not individual tool calls -- a turn that issues
    three parallel calls costs one step, which is the behaviour worth rewarding.
    """

    def __init__(
        self,
        workspace: Path | None,
        description: str,
        *,
        repo_url: str = "(local checkout)",
        model: str = DEFAULT_MODEL,
        provider: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        api_key: str | None = None,
        backend: WorkspaceBackend | None = None,
        on_step: StepCallback | None = None,
        client: LLMClient | None = None,
    ) -> None:
        # Either a local directory or a sandbox-backed workspace; the loop
        # never knows which.
        self.workspace = Path(workspace) if workspace is not None else None
        self.description = description
        self.repo_url = repo_url
        self.model = model
        self.max_steps = max_steps
        self.on_step = on_step

        if backend is None:
            if self.workspace is None:
                raise ValueError("AgentLoop needs either a workspace path or a backend")
            backend = LocalBackend(self.workspace)
        self.toolbox = Toolbox(backend)
        self.client = client or build_client(
            model=model,
            provider=provider,
            system_prompt=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            api_key=api_key,
        )

        self._result = AgentResult()

    # -- step bookkeeping -------------------------------------------------

    def _emit(
        self,
        step_type: str,
        *,
        content: str | None = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_output: dict[str, Any] | None = None,
    ) -> AgentStep:
        step = AgentStep(
            step_number=len(self._result.steps) + 1,
            type=step_type,  # type: ignore[arg-type]
            content=content,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
        )
        self._result.steps.append(step)
        if self.on_step is not None:
            try:
                self.on_step(step)
            except Exception:  # noqa: BLE001 - a failing sink must not kill the run
                logger.exception("on_step callback failed for step %s", step.step_number)
        return step

    def _fail(self, message: str) -> None:
        self._result.error = message
        self._result.stop_reason = "error"
        self._emit("observation", content=message, tool_output={"error": message})

    # -- the loop ---------------------------------------------------------

    def run(self) -> AgentResult:
        try:
            self.client.start(
                USER_PROMPT.format(repo_url=self.repo_url, description=self.description)
            )
            self._drive()
        except LLMError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - the run fails, the worker survives
            logger.exception("agent loop crashed")
            self._fail(f"{type(exc).__name__}: {exc}")
        return self._result

    def _drive(self) -> None:
        for turn in range(1, self.max_steps + 1):
            response = self.client.advance()

            self._result.input_tokens += response.input_tokens
            self._result.output_tokens += response.output_tokens
            self._result.stop_reason = response.stop_reason

            for thought in response.thoughts:
                if thought.strip():
                    self._emit("thought", content=thought)
            for text in response.text:
                if text.strip():
                    self._emit("thought", content=text)

            if response.stop_reason == "refusal":
                self._fail("The model declined to continue (content policy).")
                return

            if not response.tool_calls:
                # Nothing left to run: the model is done.
                self._result.final_message = response.joined_text
                return

            self.client.submit_tool_results(self._run_tools(response.tool_calls))

            if turn == self.max_steps:
                self._result.stop_reason = "max_steps"
                self._emit(
                    "observation",
                    content=f"Step limit reached ({self.max_steps} turns); stopping.",
                    tool_output={"stop_reason": "max_steps", "max_steps": self.max_steps},
                )

    def _run_tools(self, tool_calls: list[Any]) -> list[ToolOutcome]:
        """Execute every tool call from one turn; results go back together."""
        outcomes: list[ToolOutcome] = []
        for call in tool_calls:
            self._emit("action", tool_name=call.name, tool_input=call.arguments)

            result = self.toolbox.dispatch(call.name, call.arguments)
            self._emit(
                "observation",
                content=result.text,
                tool_name=call.name,
                tool_input=call.arguments,
                tool_output=result.payload,
            )
            outcomes.append(ToolOutcome(call=call, text=result.text, is_error=result.is_error))
        return outcomes


__all__ = ["DEFAULT_MAX_STEPS", "DEFAULT_MODEL", "DEFAULT_PROVIDER", "AgentLoop", "SYSTEM_PROMPT"]
