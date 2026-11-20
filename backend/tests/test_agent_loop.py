"""Agent loop behaviour, driven by a scripted fake model. No API key needed."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.llm import (  # noqa: E402
    LLMClient,
    LLMResponse,
    LLMToolCall,
    ToolOutcome,
    _gemini_schema,
    build_client,
    infer_provider,
)
from agent.loop import AgentLoop  # noqa: E402
from agent.tools import TOOL_DEFINITIONS  # noqa: E402


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
    """Replays a fixed list of responses, recording what was sent back."""

    provider = "scripted"

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(model="scripted", system_prompt="", tools=[])
        self.responses = list(responses)
        self.started_with: str | None = None
        self.submitted: list[list[ToolOutcome]] = []
        self.calls = 0

    def start(self, user_text: str) -> None:
        self.started_with = user_text

    def advance(self) -> LLMResponse:
        self.calls += 1
        if not self.responses:
            # Standing in for a model that keeps going forever.
            return LLMResponse(
                tool_calls=[LLMToolCall(id=f"c{self.calls}", name="list_files", arguments={"directory": "."})]
            )
        return self.responses.pop(0)

    def submit_tool_results(self, outcomes: list[ToolOutcome]) -> None:
        self.submitted.append(outcomes)


def run_with(repo: Path, responses: list[LLMResponse], **kwargs):
    client = ScriptedClient(responses)
    loop = AgentLoop(repo, "do the thing", client=client, **kwargs)
    return loop.run(), client


# -- step recording -------------------------------------------------------


def test_text_only_response_ends_the_run(repo: Path) -> None:
    result, client = run_with(repo, [LLMResponse(text=["All done."])])

    assert result.final_message == "All done."
    assert result.error is None
    assert client.calls == 1
    assert [s.type for s in result.steps] == ["thought"]


def test_tool_call_produces_action_then_observation(repo: Path) -> None:
    result, client = run_with(
        repo,
        [
            LLMResponse(
                text=["Let me look around."],
                tool_calls=[LLMToolCall(id="c1", name="list_files", arguments={"directory": "."})],
            ),
            LLMResponse(text=["Found it."]),
        ],
    )

    assert [s.type for s in result.steps] == ["thought", "action", "observation", "thought"]
    action = result.steps[1]
    assert action.tool_name == "list_files"
    assert action.tool_input == {"directory": "."}
    assert "README.md" in result.steps[2].content
    assert len(client.submitted) == 1
    assert client.submitted[0][0].is_error is False


def test_steps_are_numbered_from_one_without_gaps(repo: Path) -> None:
    result, _ = run_with(
        repo,
        [
            LLMResponse(
                tool_calls=[
                    LLMToolCall(id="a", name="read_file", arguments={"path": "README.md"}),
                    LLMToolCall(id="b", name="list_files", arguments={"directory": "."}),
                ]
            ),
            LLMResponse(text=["done"]),
        ],
    )
    assert [s.step_number for s in result.steps] == list(range(1, len(result.steps) + 1))


def test_on_step_callback_fires_live(repo: Path) -> None:
    seen: list[str] = []
    run_with(
        repo,
        [
            LLMResponse(tool_calls=[LLMToolCall(id="c1", name="list_files", arguments={"directory": "."})]),
            LLMResponse(text=["done"]),
        ],
        on_step=lambda step: seen.append(step.type),
    )
    assert seen == ["action", "observation", "thought"]


def test_failing_callback_does_not_kill_the_run(repo: Path) -> None:
    def explode(step):
        raise RuntimeError("sink is down")

    result, _ = run_with(repo, [LLMResponse(text=["done"])], on_step=explode)
    assert result.error is None
    assert result.final_message == "done"


# -- guardrails and limits ------------------------------------------------


def test_blocked_command_is_reported_back_to_the_model(repo: Path) -> None:
    result, client = run_with(
        repo,
        [
            LLMResponse(tool_calls=[LLMToolCall(id="c1", name="run_command", arguments={"command": "rm -rf /"})]),
            LLMResponse(text=["understood"]),
        ],
    )

    observation = result.steps[1]
    assert "Rejected" in observation.content
    assert client.submitted[0][0].is_error is True
    # The run itself survives: a blocked command is feedback, not a crash.
    assert result.error is None


def test_max_steps_stops_a_runaway_agent(repo: Path) -> None:
    # ScriptedClient with an empty script keeps calling tools forever.
    result, client = run_with(repo, [], max_steps=3)

    assert client.calls == 3
    assert result.stop_reason == "max_steps"
    assert "Step limit reached" in result.steps[-1].content


def test_write_then_diff_round_trip(repo: Path) -> None:
    from agent.workspace import collect_diff

    result, _ = run_with(
        repo,
        [
            LLMResponse(
                tool_calls=[
                    LLMToolCall(
                        id="c1",
                        name="write_file",
                        arguments={"path": "README.md", "content": "# Demo\n\nBadge here.\n"},
                    )
                ]
            ),
            LLMResponse(text=["Added the badge."]),
        ],
    )

    assert result.final_message == "Added the badge."
    diff = collect_diff(repo)
    assert "+Badge here." in diff


def test_refusal_is_recorded_as_an_error(repo: Path) -> None:
    result, _ = run_with(repo, [LLMResponse(stop_reason="refusal")])
    assert result.error is not None
    assert result.stop_reason == "error"


def test_token_usage_accumulates_across_turns(repo: Path) -> None:
    result, _ = run_with(
        repo,
        [
            LLMResponse(
                tool_calls=[LLMToolCall(id="c1", name="list_files", arguments={"directory": "."})],
                input_tokens=100,
                output_tokens=20,
            ),
            LLMResponse(text=["done"], input_tokens=150, output_tokens=10),
        ],
    )
    assert (result.input_tokens, result.output_tokens) == (250, 30)


# -- provider plumbing ----------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gemini-2.5-flash", "gemini"),
        ("gemini-2.5-pro", "gemini"),
        ("claude-sonnet-4-6", "anthropic"),
        ("claude-opus-5", "anthropic"),
    ],
)
def test_provider_is_inferred_from_model_name(model: str, expected: str) -> None:
    assert infer_provider(model) == expected


def test_unknown_provider_is_rejected() -> None:
    from agent.llm import LLMError

    with pytest.raises(LLMError, match="Unknown LLM provider"):
        build_client(model="gpt-4", provider="openai", system_prompt="", tools=[])


def test_gemini_schema_strips_unsupported_keywords() -> None:
    for tool in TOOL_DEFINITIONS:
        cleaned = _gemini_schema(tool["input_schema"])
        assert "additionalProperties" not in cleaned
        assert "strict" not in cleaned
        # The parts Gemini needs survive.
        assert cleaned["type"] == "object"
        assert set(cleaned["properties"]) == set(tool["input_schema"]["properties"])
        assert cleaned["required"] == tool["input_schema"]["required"]


def test_missing_gemini_key_raises_a_clear_error(monkeypatch) -> None:
    from agent.llm import LLMError

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        build_client(model="gemini-2.5-flash", system_prompt="", tools=TOOL_DEFINITIONS)


def test_gemini_client_pins_the_api_version() -> None:
    """The SDK's default API version is not stable across its own releases.

    google-genai 1.x defaulted the Developer API to v1beta; 3.x defaults it to
    v1, which rejects function calling with "Function calling is not enabled for
    api version v1". Every turn this agent takes is a tool call, so an upgrade
    that changes the default must fail here rather than on a real run.
    """
    from agent.llm import GEMINI_API_VERSION, GeminiClient

    assert GEMINI_API_VERSION == "v1beta"

    client = GeminiClient(
        model="gemini-2.5-flash",
        system_prompt="system",
        tools=TOOL_DEFINITIONS,
        api_key="not-a-real-key",
        requests_per_minute=0,  # no pacing: nothing here reaches the network
    )
    # Reaches into the SDK because the resolved version has no public accessor.
    # If that internal moves, this test fails loudly, which is the outcome worth
    # having: it is the only way to see what the client will actually send.
    resolved = client._client._api_client._http_options.api_version
    assert resolved == "v1beta", f"client would call the Gemini API at {resolved}"


# -- the thinking budget, and what an empty response reports --------------


def _gemini_client(**kwargs):
    from agent.llm import GeminiClient

    return GeminiClient(
        model="gemini-2.5-flash",
        system_prompt="system",
        tools=TOOL_DEFINITIONS,
        api_key="not-a-real-key",
        requests_per_minute=0,
        **kwargs,
    )


def _fake_response(
    *,
    finish_reason: str | None = "MAX_TOKENS",
    parts=None,
    content_missing: bool = False,
    candidates_tokens=None,
    thoughts_tokens=None,
    prompt_tokens=13_808,
    block_reason=None,
):
    """A generate_content result shaped like the ones that broke a real run."""
    content = None if content_missing else SimpleNamespace(parts=parts)
    candidate = SimpleNamespace(content=content, finish_reason=finish_reason)
    return SimpleNamespace(
        candidates=[candidate],
        prompt_feedback=SimpleNamespace(block_reason=block_reason),
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
            thoughts_token_count=thoughts_tokens,
            total_token_count=(prompt_tokens or 0)
            + (candidates_tokens or 0)
            + (thoughts_tokens or 0),
        ),
    )


def test_thinking_budget_is_configured_by_default() -> None:
    """Thinking is drawn from max_output_tokens, so it needs a ceiling: with
    none, a long deliberation can leave nothing for the answer."""
    from agent.llm import DEFAULT_GEMINI_THINKING_BUDGET

    assert DEFAULT_GEMINI_THINKING_BUDGET == 2048
    client = _gemini_client()
    assert client._config.thinking_config.thinking_budget == 2048
    assert client._config.max_output_tokens == 8192


def test_thinking_budget_reads_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_THINKING_BUDGET", "512")
    assert _gemini_client()._config.thinking_config.thinking_budget == 512


def test_an_explicit_thinking_budget_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_THINKING_BUDGET", "512")
    assert _gemini_client(thinking_budget=0)._config.thinking_config.thinking_budget == 0


def test_an_empty_parts_response_fails_the_turn(monkeypatch) -> None:
    """Previously this returned quietly.

    A MAX_TOKENS response whose budget all went to thinking arrives with content
    set and `parts` empty. The old loop iterated nothing, set no final message
    and raised nothing, so the run was stored as completed having done nothing.
    """
    from agent.llm import LLMError

    client = _gemini_client()
    monkeypatch.setattr(
        client._client.models,
        "generate_content",
        lambda **_kw: _fake_response(parts=[], candidates_tokens=None, thoughts_tokens=8192),
    )
    with pytest.raises(LLMError) as caught:
        client.advance()
    assert "MAX_TOKENS" in str(caught.value)


def test_a_failed_turn_records_why(monkeypatch) -> None:
    """The diagnostics are the whole point: without them the only record of a
    run dying here was "block_reason=None", which says nothing."""
    from agent.llm import LLMError

    client = _gemini_client()
    monkeypatch.setattr(
        client._client.models,
        "generate_content",
        lambda **_kw: _fake_response(
            finish_reason="MAX_TOKENS", parts=[], thoughts_tokens=8192, prompt_tokens=13_808
        ),
    )
    with pytest.raises(LLMError) as caught:
        client.advance()

    details = caught.value.details
    assert details["finish_reason"] == "MAX_TOKENS"
    assert details["thoughts_tokens"] == 8192
    assert details["prompt_tokens"] == 13_808
    assert details["part_count"] == 0
    assert details["max_output_tokens"] == 8192
    assert details["thinking_budget"] == 2048

    message = str(caught.value)
    assert "finish_reason=MAX_TOKENS" in message
    assert "thinking_tokens=8192" in message
    # block_reason is a safety field; naming it for a budget failure was noise.
    assert "block_reason" not in message


def test_a_safety_block_still_names_the_block_reason(monkeypatch) -> None:
    from agent.llm import LLMError

    client = _gemini_client()
    monkeypatch.setattr(
        client._client.models,
        "generate_content",
        lambda **_kw: _fake_response(
            finish_reason="SAFETY", content_missing=True, block_reason="SAFETY"
        ),
    )
    with pytest.raises(LLMError) as caught:
        client.advance()
    assert "finish_reason=SAFETY" in str(caught.value)
    assert "block_reason=SAFETY" in str(caught.value)


def test_a_normal_response_still_works(monkeypatch) -> None:
    """The empty-parts guard must not reject a healthy turn."""
    client = _gemini_client()
    part = SimpleNamespace(function_call=None, text="all done", thought=False)
    monkeypatch.setattr(
        client._client.models,
        "generate_content",
        lambda **_kw: _fake_response(
            finish_reason="STOP", parts=[part], candidates_tokens=12, thoughts_tokens=30
        ),
    )
    response = client.advance()
    assert response.text == ["all done"]
    assert response.stop_reason == "end_turn"
    # Thinking is counted as output spend, because that is the budget it uses.
    assert response.output_tokens == 42


# -- rate limiting and retries -------------------------------------------


def test_rate_limiter_spaces_requests(monkeypatch) -> None:
    from agent import llm

    clock = {"now": 0.0}
    slept: list[float] = []
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(llm.time, "sleep", lambda s: (slept.append(s), clock.__setitem__("now", clock["now"] + s)))

    limiter = llm.RateLimiter(requests_per_minute=5)  # one request per 12s
    limiter.wait()
    assert slept == []

    clock["now"] += 2.0  # only 2s have passed
    limiter.wait()
    assert slept == [pytest.approx(10.0)]

    clock["now"] += 30.0  # well past the interval
    limiter.wait()
    assert len(slept) == 1


def test_rate_limiter_disabled_at_zero(monkeypatch) -> None:
    from agent import llm

    monkeypatch.setattr(llm.time, "sleep", lambda s: pytest.fail("should not sleep"))
    limiter = llm.RateLimiter(requests_per_minute=0)
    limiter.wait()
    limiter.wait()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("429 RESOURCE_EXHAUSTED. Please retry in 3.396s.", 4.396),
        ("{'retryDelay': '31s'}", 32.0),
        ("plain 500 error", 0.0),
    ],
)
def test_server_retry_delay_is_parsed(message: str, expected: float) -> None:
    from agent.llm import _server_retry_delay

    assert _server_retry_delay(Exception(message)) == pytest.approx(expected)


def test_retry_waits_at_least_as_long_as_the_server_asks(monkeypatch) -> None:
    from agent import llm

    slept: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    monkeypatch.setattr(llm.random, "uniform", lambda a, b: 0.0)

    class Quota(Exception):
        code = 429

        def __str__(self) -> str:
            return "429 RESOURCE_EXHAUSTED. Please retry in 30s."

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Quota()
        return "ok"

    assert llm._retry(flaky, "test") == "ok"
    # Local backoff would have been 1s then 2s; the server's 30s wins.
    assert slept == [pytest.approx(31.0), pytest.approx(31.0)]


def test_retry_does_not_swallow_non_retryable_errors(monkeypatch) -> None:
    from agent import llm

    monkeypatch.setattr(llm.time, "sleep", lambda s: pytest.fail("should not retry a 400"))

    class BadRequest(Exception):
        code = 400

    with pytest.raises(BadRequest):
        llm._retry(lambda: (_ for _ in ()).throw(BadRequest()), "test")
