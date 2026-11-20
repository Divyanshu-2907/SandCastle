"""Tracing behaviour, driven by a fake tracer and the scripted fake model.

No Langfuse account and no network: these cover the contract `AgentLoop` relies
on -- that a tracer is called the right number of times with the right numbers,
and that a broken tracer costs nothing.
"""

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.llm import LLMClient, LLMError, LLMResponse, LLMToolCall, ToolOutcome  # noqa: E402
from agent.loop import AgentLoop  # noqa: E402
from agent.tracing import (  # noqa: E402
    COUNT_KEY_NAMES,
    REDACTED,
    NoopTracer,
    build_tracer,
    redact,
    redact_arguments,
    run_completed_score,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Demo\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)

    git("init", "--quiet")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "--quiet", "-m", "initial")
    return workspace


class ScriptedClient(LLMClient):
    """Replays a fixed list of responses; then asks for a tool forever."""

    provider = "scripted"

    def __init__(self, responses: list[LLMResponse], raise_on_advance: bool = False) -> None:
        super().__init__(model="scripted", system_prompt="", tools=[])
        self.responses = list(responses)
        self.raise_on_advance = raise_on_advance
        self.calls = 0

    def start(self, user_text: str) -> None:
        return None

    def advance(self) -> LLMResponse:
        self.calls += 1
        if self.raise_on_advance:
            raise LLMError("the provider is down")
        if self.responses:
            return self.responses.pop(0)
        return LLMResponse(
            tool_calls=[
                LLMToolCall(id=f"c{self.calls}", name="list_files", arguments={"directory": "."})
            ],
            stop_reason="tool_use",
        )

    def submit_tool_results(self, outcomes: list[ToolOutcome]) -> None:
        return None


class FakeTracer:
    """Records every call. `explode` makes every method raise instead."""

    def __init__(self, explode: bool = False) -> None:
        self.explode = explode
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _maybe_raise(self) -> None:
        if self.explode:
            raise RuntimeError("tracer is broken")

    def _log(self, kind: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((kind, kwargs))

    def start_run(self, **kwargs: Any) -> None:
        self._log("start_run", kwargs)
        self._maybe_raise()

    @contextmanager
    def turn(self, **kwargs: Any):
        self._log("turn", kwargs)
        self._maybe_raise()
        yield self

    @contextmanager
    def tool(self, **kwargs: Any):
        self._log("tool", kwargs)
        self._maybe_raise()
        yield self

    def record(self, **kwargs: Any) -> None:
        self._log("record", kwargs)
        self._maybe_raise()

    def end_run(self, **kwargs: Any) -> None:
        self._log("end_run", kwargs)
        self._maybe_raise()

    def flush(self) -> None:
        self._maybe_raise()

    # -- helpers for assertions ------------------------------------------

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)

    def only(self, kind: str) -> dict[str, Any]:
        matches = [kwargs for name, kwargs in self.calls if name == kind]
        assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
        return matches[0]

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == kind]


def drive(repo: Path, responses: list[LLMResponse], **kwargs: Any):
    """Run the loop with a fake tracer; returns (result, tracer)."""
    tracer = FakeTracer(explode=kwargs.pop("explode", False))
    client = ScriptedClient(responses, raise_on_advance=kwargs.pop("raise_on_advance", False))
    result = AgentLoop(
        workspace=repo,
        description="do the thing",
        repo_url="https://example.com/r.git",
        model="scripted-model",
        max_steps=kwargs.pop("max_steps", 5),
        client=client,
        tracer=tracer,
        run_id="run-abc",
        **kwargs,
    ).run()
    return result, tracer


# One turn that calls two tools (one of them blocked by a guardrail), then a
# turn that finishes. Two turns, two tool calls.
TWO_TURNS = [
    LLMResponse(
        text=["looking around"],
        tool_calls=[
            LLMToolCall(id="c1", name="list_files", arguments={"directory": "."}),
            LLMToolCall(id="c2", name="run_command", arguments={"command": "curl http://evil"}),
        ],
        stop_reason="tool_use",
        input_tokens=100,
        output_tokens=20,
    ),
    LLMResponse(text=["all done"], stop_reason="end_turn", input_tokens=50, output_tokens=10),
]


# -- call counts and scores ----------------------------------------------


def test_one_trace_per_run_with_a_span_per_turn_and_tool(repo: Path) -> None:
    result, tracer = drive(repo, TWO_TURNS)

    assert result.error is None
    assert tracer.count("start_run") == 1
    assert tracer.count("turn") == 2
    assert tracer.count("tool") == 2
    assert tracer.count("end_run") == 1

    assert [call["name"] for call in tracer.of_kind("tool")] == ["list_files", "run_command"]
    assert [call["number"] for call in tracer.of_kind("turn")] == [1, 2]


def test_start_run_carries_the_task_and_the_model(repo: Path) -> None:
    _, tracer = drive(repo, TWO_TURNS)

    start = tracer.only("start_run")
    assert start["run_id"] == "run-abc"
    assert start["description"] == "do the thing"
    assert start["repo_url"] == "https://example.com/r.git"
    assert start["model"] == "scripted-model"
    assert start["provider"] == "scripted"


def test_end_run_scores_a_finished_run(repo: Path) -> None:
    _, tracer = drive(repo, TWO_TURNS)

    end = tracer.only("end_run")
    assert end["stop_reason"] == "end_turn"
    assert end["error"] is None
    assert run_completed_score(end["error"], end["stop_reason"]) == 1
    # Model turns, not emitted steps: one turn produced several steps.
    assert end["steps_used"] == 2
    assert end["guardrail_block_count"] == 1
    # The blocked call is counted as a block and not also as a tool error.
    assert end["tool_error_count"] == 0
    assert end["input_tokens"] == 150
    assert end["output_tokens"] == 30


def test_a_failing_tool_counts_as_an_error_not_a_block(repo: Path) -> None:
    responses = [
        LLMResponse(
            tool_calls=[
                # Reading a file that is not there fails in the tool itself.
                LLMToolCall(id="c1", name="read_file", arguments={"path": "nope.txt"})
            ],
            stop_reason="tool_use",
        ),
        LLMResponse(text=["done"], stop_reason="end_turn"),
    ]
    _, tracer = drive(repo, responses)

    end = tracer.only("end_run")
    assert end["tool_error_count"] == 1
    assert end["guardrail_block_count"] == 0

    recorded = [call for call in tracer.of_kind("record") if "blocked" in call]
    assert len(recorded) == 1
    assert recorded[0]["is_error"] is True
    assert recorded[0]["blocked"] is False


def test_a_blocked_tool_is_recorded_as_blocked(repo: Path) -> None:
    _, tracer = drive(repo, TWO_TURNS)

    blocked = [call for call in tracer.of_kind("record") if call.get("blocked")]
    assert len(blocked) == 1
    assert blocked[0]["is_error"] is True


def test_hitting_the_step_limit_scores_zero(repo: Path) -> None:
    # No scripted responses: the client asks for a tool forever.
    result, tracer = drive(repo, [], max_steps=3)

    end = tracer.only("end_run")
    assert result.stop_reason == "max_steps"
    assert end["steps_used"] == 3
    assert run_completed_score(end["error"], end["stop_reason"]) == 0


def test_a_failed_turn_reports_the_providers_diagnostics(repo: Path) -> None:
    """The failing call is the one worth understanding, and it is the only one
    with no response to read numbers off. They ride on the exception instead."""
    details = {
        "finish_reason": "MAX_TOKENS",
        "thoughts_tokens": 8192,
        "prompt_tokens": 13808,
        "part_count": 0,
    }

    class FailingClient(ScriptedClient):
        def advance(self) -> LLMResponse:
            raise LLMError("Gemini returned no usable content", details)

    tracer = FakeTracer()
    result = AgentLoop(
        workspace=repo,
        description="d",
        model="m",
        max_steps=3,
        client=FailingClient([]),
        tracer=tracer,
        run_id="run-details",
    ).run()

    assert result.error is not None
    recorded = [c for c in tracer.of_kind("record") if c.get("details")]
    assert len(recorded) == 1, "the failing turn recorded no diagnostics"
    assert recorded[0]["details"]["finish_reason"] == "MAX_TOKENS"
    assert recorded[0]["details"]["thoughts_tokens"] == 8192
    assert recorded[0]["stop_reason"] == "error"


def test_thinking_tokens_are_reported_separately_on_a_good_turn(repo: Path) -> None:
    """Thinking is counted inside output because that is the budget it spends,
    but it is invisible in the transcript -- a turn can burn most of its output
    on thinking and show almost nothing, and the two have to be told apart."""
    responses = [
        LLMResponse(
            text=["done"],
            stop_reason="end_turn",
            input_tokens=620,
            output_tokens=500,
            thinking_tokens=430,
        )
    ]
    _, tracer = drive(repo, responses)

    turn = next(c for c in tracer.of_kind("record") if "thinking_tokens" in c)
    assert turn["thinking_tokens"] == 430
    assert turn["output_tokens"] == 500

    end = tracer.only("end_run")
    assert end["thinking_tokens"] == 430


def test_turn_usage_reports_thinking_from_a_good_turn() -> None:
    from agent.tracing import _turn_usage, _TurnValues

    values = _TurnValues()
    values.record(model="m", input_tokens=620, output_tokens=500, thinking_tokens=430)
    assert _turn_usage(values) == {"input": 620, "output": 500, "thinking": 430}


def test_turn_usage_falls_back_to_the_failure_details() -> None:
    """A turn that burned 8,192 thinking tokens and returned nothing must not
    be recorded as having spent zero."""
    from agent.tracing import _turn_usage, _TurnValues

    values = _TurnValues()
    values.record(
        model="m",
        details={"prompt_tokens": 13808, "candidates_tokens": None, "thoughts_tokens": 8192},
    )
    usage = _turn_usage(values)
    assert usage["input"] == 13808
    assert usage["output"] == 8192
    assert usage["thinking"] == 8192


def test_end_run_fires_once_when_the_provider_fails(repo: Path) -> None:
    """The crash path is the one where an unclosed trace would matter most."""
    result, tracer = drive(repo, [], raise_on_advance=True)

    assert result.error == "the provider is down"
    assert tracer.count("start_run") == 1
    assert tracer.count("end_run") == 1
    end = tracer.only("end_run")
    assert end["error"] == "the provider is down"
    assert run_completed_score(end["error"], end["stop_reason"]) == 0


# -- a broken tracer must cost nothing -----------------------------------


def test_a_tracer_that_raises_everywhere_does_not_fail_the_run(repo: Path) -> None:
    result, tracer = drive(repo, TWO_TURNS, explode=True)

    assert result.error is None
    assert result.stop_reason == "end_turn"
    assert result.final_message == "all done"
    # The work still happened: both tools ran and every step was emitted.
    assert result.input_tokens == 150
    assert [step.tool_name for step in result.steps if step.type == "action"] == [
        "list_files",
        "run_command",
    ]
    # And the loop still tried to trace all of it.
    assert tracer.count("start_run") == 1
    assert tracer.count("turn") == 2
    assert tracer.count("tool") == 2
    assert tracer.count("end_run") == 1


def test_a_tracer_cannot_swallow_a_real_failure(repo: Path) -> None:
    """A tracer's __exit__ returning True must not suppress the body."""

    class SwallowingTracer(FakeTracer):
        @contextmanager
        def turn(self, **kwargs: Any):
            self._log("turn", kwargs)
            try:
                yield self
            except Exception:
                return  # swallow, which the guard must override

    tracer = SwallowingTracer()
    result = AgentLoop(
        workspace=repo,
        description="d",
        model="m",
        max_steps=3,
        client=ScriptedClient([], raise_on_advance=True),
        tracer=tracer,
        run_id="run-xyz",
    ).run()

    assert result.error == "the provider is down"


def test_the_default_tracer_is_a_noop(repo: Path) -> None:
    loop = AgentLoop(workspace=repo, description="d", model="m", client=ScriptedClient([]))
    assert isinstance(loop.tracer, NoopTracer)


def test_a_run_id_is_minted_when_none_is_given(repo: Path) -> None:
    loop = AgentLoop(workspace=repo, description="d", model="m", client=ScriptedClient([]))
    assert loop.run_id
    other = AgentLoop(workspace=repo, description="d", model="m", client=ScriptedClient([]))
    assert loop.run_id != other.run_id


# -- build_tracer ---------------------------------------------------------


@pytest.mark.parametrize(
    "public_key,secret_key",
    [("", ""), ("pk-lf-x", ""), ("", "sk-lf-y"), ("   ", "sk-lf-y")],
)
def test_build_tracer_is_a_noop_without_both_keys(public_key: str, secret_key: str) -> None:
    tracer = build_tracer(public_key=public_key, secret_key=secret_key, host="")
    assert isinstance(tracer, NoopTracer)


def test_build_tracer_ignores_the_environment_when_given_keys(monkeypatch) -> None:
    """Explicit empty settings must win over stray environment variables."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-from-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-from-env")
    assert isinstance(build_tracer(public_key="", secret_key="", host=""), NoopTracer)


def test_build_tracer_reads_the_environment_when_asked(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert isinstance(build_tracer(), NoopTracer)


def test_noop_tracer_satisfies_the_loop_without_a_run(repo: Path) -> None:
    tracer = NoopTracer()
    tracer.start_run(run_id="r", description="d", repo_url="u", model="m", provider="p")
    with tracer.turn(number=1) as sink:
        sink.record(model="m", input_tokens=1, output_tokens=2)
    with tracer.tool(name="read_file", arguments={"path": "a"}) as sink:
        sink.record(text="x", is_error=False, blocked=False)
    tracer.end_run(
        final_message=None,
        stop_reason="end_turn",
        error=None,
        steps_used=1,
        tool_error_count=0,
        guardrail_block_count=0,
    )
    tracer.flush()


# -- redaction ------------------------------------------------------------


# Every sample below is assembled from fragments at import time rather than
# written as one literal. Secret scanners -- GitHub push protection among them
# -- match on shape, not on whether a credential is real, so one complete
# token-shaped literal in a test file is enough to block a push. The values the
# tests exercise are byte-for-byte what they were; only the spelling in the
# source changes, so none of these checks got weaker.


def _assemble(*parts: str) -> str:
    return "".join(parts)


FAKE_CREDENTIALS: dict[str, str] = {
    "anthropic": _assemble("sk-", "ant-", "api03-", "a" * 29),
    "aws": _assemble("AKIA", "IOSFODNN7", "EXAMPLE"),
    "github": _assemble("ghp", "_", "abcdefghijklmnop", "qrstuvwxyz0123"),
    "google": _assemble("AIza", "SyA1b2C3d4E5f6", "G7h8I9j0K1l2", "M3n4O5p6Q"),
    "jwt": ".".join(
        (
            _assemble("eyJhbGciOi", "JIUzI1NiJ9"),
            _assemble("eyJzdWIiOi", "IxMjM0NTYifQ"),
            _assemble("dozjgNryP4J3", "jVmNHl0w5N"),
        )
    ),
    "openai": _assemble("sk-", "proj-", "abcdefghijklmnop", "qrstuvwxyz0123"),
    "slack": _assemble("xox", "b-", "123456789012-", "abcdefghijklmnop"),
}

PEM_BODY = "MIIEowIBAAKCAQEA"
FAKE_PEM = "\n".join(
    (
        _assemble("-----BEGIN ", "RSA PRIVATE", " KEY-----"),
        PEM_BODY,
        _assemble("-----END ", "RSA PRIVATE", " KEY-----"),
    )
)


def test_every_credential_shape_is_actually_matched() -> None:
    """Guards the assembly above.

    A fragment joined wrongly would produce a string the patterns no longer
    recognise, and `test_credentials_never_reach_a_span` would then pass while
    testing nothing -- the sample would be absent from the output because it was
    never a credential shape in the first place.
    """
    for kind, secret in FAKE_CREDENTIALS.items():
        assert redact(secret) == REDACTED, f"{kind} sample no longer matches a pattern"


@pytest.mark.parametrize("kind", sorted(FAKE_CREDENTIALS))
def test_credentials_never_reach_a_span(kind: str) -> None:
    secret = FAKE_CREDENTIALS[kind]
    assert secret not in redact(f"the value is {secret} ok")
    assert secret not in str(redact({"command": f"curl -H 'X: {secret}'"}))
    assert secret not in str(redact_arguments({"content": secret}))


def test_private_keys_are_removed_whole() -> None:
    assert PEM_BODY in FAKE_PEM  # the sample really does carry a key body
    assert PEM_BODY not in redact(FAKE_PEM)


def test_assignments_keep_the_name_and_lose_the_value() -> None:
    out = redact("password: hunter2hunter2")
    assert "hunter2hunter2" not in out
    assert "password" in out


def test_secret_named_fields_are_replaced_wholesale() -> None:
    out = redact({"api_key": "short", "nested": [{"secret": "alsoshort"}], "path": "src/a.py"})
    assert out["api_key"] == REDACTED
    assert out["nested"][0]["secret"] == REDACTED
    assert out["path"] == "src/a.py"


def test_bytes_do_not_smuggle_a_secret_past_redaction() -> None:
    """A bytes value is a Sequence of ints; iterating it would hand over the
    credential as character codes, scrubbed by nothing."""
    out = redact({"blob": FAKE_CREDENTIALS["google"].encode("utf-8")})
    assert out["blob"] == REDACTED


def test_token_counts_survive_redaction() -> None:
    """`input_tokens` contains "token"; without the exemption the usage figures
    tracing exists to collect would arrive as "[redacted]"."""
    out = redact({"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})
    assert out == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    for name in COUNT_KEY_NAMES:
        assert redact({name: 7})[name] == 7


def test_every_diagnostic_field_survives_redaction() -> None:
    """The guard that generalises the token-count exemption.

    `candidates_tokens` contains "token" and was being masked to "[redacted]"
    in real traces -- the exact data the failure path exists to capture. Asserts
    against the diagnostics the Gemini client actually emits, so adding a field
    whose name trips the secret matcher fails here rather than in production.
    """
    from agent.llm import _gemini_diagnostics

    diagnostics = _gemini_diagnostics(None, None, thinking_budget=2048)
    # Fill every slot with a number so a masked one is unmistakable.
    sample = {key: 123 for key in diagnostics}
    out = redact(sample)
    masked = sorted(k for k, v in out.items() if v == REDACTED)
    assert not masked, f"diagnostics lost to the mask: {masked}"


def test_long_tool_arguments_are_truncated() -> None:
    out = redact_arguments({"path": "big.py", "content": "x" * 60_000})
    assert len(out["content"]) < 60_000
    assert "characters truncated" in out["content"]
    assert out["path"] == "big.py"


def test_run_completed_score_rules() -> None:
    assert run_completed_score(None, "end_turn") == 1
    assert run_completed_score(None, "max_steps") == 0
    assert run_completed_score(None, "refusal") == 0
    assert run_completed_score("boom", "end_turn") == 0
