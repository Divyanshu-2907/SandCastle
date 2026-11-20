"""Provider-agnostic chat layer for the agent loop.

The loop only ever sees `LLMResponse` / `LLMToolCall`, so swapping providers is a
config change. Google Gemini is the default (generous free tier); Anthropic is
kept behind the same interface for when a stronger model is worth paying for.

Each client owns its own conversation state because the wire formats disagree on
how an assistant turn and its tool results are represented, and both providers
require their own turn echoed back verbatim.
"""

import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "gemini"
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-6",
}
MAX_OUTPUT_TOKENS = 8_192

# Gemini free-tier quotas are metered per minute AND per day, both per model, and
# an agent run is a burst of requests. Two defences:
#   1. A pacer that spaces requests out so the quota is never tripped.
#   2. Retries that honour the server's own retryDelay -- exponential backoff
#      alone tops out well under a minute and just burns the remaining attempts.
MAX_RETRIES = 6
MAX_BACKOFF = 64
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 0 disables pacing (paid tiers, or another provider).
DEFAULT_GEMINI_RPM = 10

# Pinned rather than left to the SDK's default, which is not stable across
# releases: google-genai 1.x defaulted the Gemini Developer API to v1beta, and
# 3.x defaults it to v1 -- where function calling is rejected outright with
# "Function calling is not enabled for api version v1". The whole agent is tool
# calls, so the version it talks is a requirement of this code, not a detail to
# inherit from whatever version of the SDK happens to be installed.
GEMINI_API_VERSION = "v1beta"

# Thinking tokens are drawn from the same max_output_tokens budget as the visible
# answer, and they are not small: measured at 1,487 thinking against 1,461 visible
# on a trivial prompt. Starve the budget and thinking takes all of it -- at 30
# tokens the model spent 27 on thinking, returned no content, and finished
# MAX_TOKENS. Capping thinking keeps room for an answer no matter how long the
# model deliberates. 0 disables thinking; -1 hands the budget back to the model.
DEFAULT_GEMINI_THINKING_BUDGET = 2048


class LLMError(Exception):
    """Unrecoverable failure talking to the model provider.

    `details` carries whatever the provider said about *why*, for a failure that
    happens before there is an `LLMResponse` to put it on. A run that died on an
    empty candidate used to record nothing at all -- no finish reason, no token
    counts -- so the one question worth asking afterwards had no answer.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = details or {}


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolOutcome:
    """A tool result on its way back to the model."""

    call: LLMToolCall
    text: str
    is_error: bool = False


@dataclass(slots=True)
class LLMResponse:
    thoughts: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def joined_text(self) -> str | None:
        return "\n".join(t for t in self.text if t.strip()).strip() or None


class LLMClient(ABC):
    """One conversation with one model."""

    provider: str

    def __init__(self, model: str, system_prompt: str, tools: list[dict[str, Any]]) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools

    @abstractmethod
    def start(self, user_text: str) -> None:
        """Begin a fresh conversation with the opening user message."""

    @abstractmethod
    def advance(self) -> LLMResponse:
        """One round-trip. Records the assistant turn in the conversation."""

    @abstractmethod
    def submit_tool_results(self, outcomes: list[ToolOutcome]) -> None:
        """Append every tool result from one turn as a single conversation turn."""


def _retry(call, describe: str):
    """Retry `call` through rate limits and transient 5xx.

    Prefers the server's advertised retry delay over local backoff: a per-minute
    quota needs a wait measured in tens of seconds, which exponential backoff
    only reaches after it has already exhausted its attempts.
    """
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable
            status = _status_of(exc)
            if status not in RETRYABLE_STATUS or attempt == MAX_RETRIES - 1:
                raise
            last = exc
            backoff = min(2**attempt, MAX_BACKOFF)
            delay = max(backoff, _server_retry_delay(exc)) + random.uniform(0, 1)
            logger.warning(
                "%s failed with status %s; retrying in %.1fs (attempt %d/%d)",
                describe,
                status,
                delay,
                attempt + 1,
                MAX_RETRIES,
            )
            time.sleep(delay)
    raise LLMError(f"{describe} failed after {MAX_RETRIES} attempts: {last}")


def _status_of(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


# Matches both shapes Gemini uses: "'retryDelay': '31s'" and "Please retry in 3.4s".
_RETRY_DELAY_RE = re.compile(
    r"retry(?:Delay)?['\"]?\s*(?:in\s+)?[:\s]*['\"]?(\d+(?:\.\d+)?)\s*s", re.I
)


def _server_retry_delay(exc: Exception) -> float:
    """Seconds the server asked us to wait, plus a second of margin. 0 if silent."""
    match = _RETRY_DELAY_RE.search(str(exc))
    if match is None:
        return 0.0
    return min(float(match.group(1)) + 1.0, MAX_BACKOFF)


class RateLimiter:
    """Spaces requests so a per-minute quota is never tripped in the first place."""

    def __init__(self, requests_per_minute: int) -> None:
        self.min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._last_request: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        if self._last_request is not None:
            remaining = self.min_interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                logger.debug("pacing: sleeping %.1fs before next request", remaining)
                time.sleep(remaining)
        self._last_request = time.monotonic()


# -- Gemini ---------------------------------------------------------------


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip JSON Schema keywords Gemini's validator rejects."""
    cleaned = {k: v for k, v in schema.items() if k not in {"additionalProperties", "strict"}}
    if "properties" in cleaned:
        cleaned["properties"] = {
            name: _gemini_schema(sub) if isinstance(sub, dict) else sub
            for name, sub in cleaned["properties"].items()
        }
    return cleaned


_QUOTA_ID_RE = re.compile(r"'quotaId':\s*'([^']+)'")
_QUOTA_VALUE_RE = re.compile(r"'quotaValue':\s*'(\d+)'")


def _describe_gemini_error(exc: Exception, model: str) -> str:
    """Turn a wall-of-JSON provider error into one actionable line.

    Quota failures are the common case on the free tier, and the raw error buries
    the one thing that matters: which quota, and how big it is.
    """
    text = str(exc)
    status = _status_of(exc)

    if status == 429:
        quota_match = _QUOTA_ID_RE.search(text)
        limit_match = _QUOTA_VALUE_RE.search(text)
        quota_id = quota_match.group(1) if quota_match else ""
        limit = limit_match.group(1) if limit_match else "?"
        period = "day" if "PerDay" in quota_id else "minute"
        advice = (
            f"The free-tier quota for {model} is exhausted ({limit} requests per {period}). "
        )
        if period == "day":
            advice += (
                "Daily quotas are per-model, so switching AGENT_MODEL to another model "
                "(e.g. gemini-2.5-flash) gives a fresh allowance; otherwise wait for the "
                "quota to reset."
            )
        else:
            advice += "Lower GEMINI_RPM so requests are spaced further apart."
        return advice

    if status == 404 and "no longer available" in text:
        return f"Model {model} has been retired by Google. Set AGENT_MODEL to a current model."
    if status in (401, 403):
        return f"Gemini rejected the credentials for {model}. Check GEMINI_API_KEY."
    return f"{type(exc).__name__} calling {model}: {text[:400]}"


class GeminiClient(LLMClient):
    provider = "gemini"

    def __init__(
        self,
        model: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        api_key: str | None = None,
        requests_per_minute: int | None = None,
        thinking_budget: int | None = None,
    ) -> None:
        super().__init__(model, system_prompt, tools)

        from google import genai
        from google.genai import types

        self._types = types
        key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set.")
        self._client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(api_version=GEMINI_API_VERSION),
        )

        rpm = (
            requests_per_minute
            if requests_per_minute is not None
            else int(os.getenv("GEMINI_RPM", DEFAULT_GEMINI_RPM))
        )
        self._limiter = RateLimiter(rpm)

        declarations = [
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                parameters_json_schema=_gemini_schema(tool["input_schema"]),
            )
            for tool in tools
        ]
        self._thinking_budget = (
            thinking_budget
            if thinking_budget is not None
            else int(os.getenv("GEMINI_THINKING_BUDGET", DEFAULT_GEMINI_THINKING_BUDGET))
        )
        self._config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=declarations)],
            # The SDK will happily call Python functions for us; we need the raw
            # calls so every one passes a guardrail check first.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=MAX_OUTPUT_TOKENS,
            # Bounds the share of max_output_tokens thinking may take, so a long
            # deliberation cannot leave zero room for the answer or the tool call.
            thinking_config=types.ThinkingConfig(thinking_budget=self._thinking_budget),
        )
        self._contents: list[Any] = []
        self._call_names: dict[str, str] = {}

    def start(self, user_text: str) -> None:
        types = self._types
        self._contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_text)])]

    def advance(self) -> LLMResponse:
        def request():
            self._limiter.wait()
            return self._client.models.generate_content(
                model=self.model, contents=self._contents, config=self._config
            )

        try:
            raw = _retry(request, f"gemini {self.model}")
        except Exception as exc:  # noqa: BLE001 - normalised into LLMError below
            raise LLMError(_describe_gemini_error(exc, self.model)) from exc

        candidate = (raw.candidates or [None])[0]
        content = getattr(candidate, "content", None)
        parts = list(getattr(content, "parts", None) or [])
        # Empty `parts` counts as a failure, not a quiet finish. A MAX_TOKENS
        # response whose whole budget went to thinking arrives with content set
        # and parts empty; iterating it produced no steps, no final message and
        # no error, so the run was recorded as completed having done nothing.
        if candidate is None or content is None or not parts:
            details = _gemini_diagnostics(raw, candidate, self._thinking_budget)
            raise LLMError(_describe_empty_gemini_response(details), details)

        # Echo the model's own turn back verbatim on the next request.
        self._contents.append(content)

        response = LLMResponse(stop_reason=_gemini_stop_reason(candidate.finish_reason))
        for part in parts:
            if part.function_call is not None:
                call = part.function_call
                call_id = call.id or f"{call.name}-{len(self._call_names)}"
                self._call_names[call_id] = call.name or ""
                response.tool_calls.append(
                    LLMToolCall(id=call_id, name=call.name or "", arguments=dict(call.args or {}))
                )
            elif part.text:
                (response.thoughts if part.thought else response.text).append(part.text)

        usage = raw.usage_metadata
        if usage is not None:
            response.input_tokens = usage.prompt_token_count or 0
            response.output_tokens = (usage.candidates_token_count or 0) + (
                usage.thoughts_token_count or 0
            )
        return response

    def submit_tool_results(self, outcomes: list[ToolOutcome]) -> None:
        types = self._types
        parts = [
            types.Part.from_function_response(
                name=outcome.call.name,
                response={"error" if outcome.is_error else "output": outcome.text},
            )
            for outcome in outcomes
        ]
        self._contents.append(types.Content(role="user", parts=parts))


def _enum_name(value: Any) -> str | None:
    """`FinishReason.MAX_TOKENS` -> "MAX_TOKENS"; passes plain strings through."""
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _gemini_diagnostics(raw: Any, candidate: Any, thinking_budget: int) -> dict[str, Any]:
    """Everything the response says about why it carried nothing usable.

    Read off the raw response rather than an `LLMResponse`, because the failure
    happens before one exists. Every lookup is defensive: this runs on the error
    path, where the shape is by definition not what was expected.
    """
    usage = getattr(raw, "usage_metadata", None)
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None)
    return {
        "finish_reason": _enum_name(getattr(candidate, "finish_reason", None)),
        "block_reason": _enum_name(getattr(getattr(raw, "prompt_feedback", None), "block_reason", None)),
        "candidate_count": len(getattr(raw, "candidates", None) or []),
        "has_content": content is not None,
        "part_count": len(parts or []),
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "candidates_tokens": getattr(usage, "candidates_token_count", None),
        "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "thinking_budget": thinking_budget,
    }


def _describe_empty_gemini_response(details: dict[str, Any]) -> str:
    """One line naming the actual reason.

    The old message reported `block_reason`, which is a *safety* field and is
    None for every failure that is not a safety block -- so it said nothing at
    all about the common cases. Lead with the finish reason instead.
    """
    finish = details.get("finish_reason") or "unknown"
    parts = [f"Gemini returned no usable content (finish_reason={finish}"]
    thoughts = details.get("thoughts_tokens")
    if thoughts is not None:
        parts.append(f", thinking_tokens={thoughts}/{details.get('thinking_budget')}")
    visible = details.get("candidates_tokens")
    if visible is not None:
        parts.append(f", visible_tokens={visible}")
    if details.get("block_reason"):
        parts.append(f", block_reason={details['block_reason']}")
    parts.append(").")
    if finish == "MAX_TOKENS":
        parts.append(
            " The output budget ran out before the model produced anything;"
            " thinking tokens are drawn from the same budget."
        )
    elif finish in {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS"}:
        parts.append(" The model's tool call was rejected by the API before it reached us.")
    return "".join(parts)


def _gemini_stop_reason(finish_reason: Any) -> str:
    if finish_reason is None:
        return "end_turn"
    name = getattr(finish_reason, "name", str(finish_reason)).upper()
    return {
        "STOP": "end_turn",
        "MAX_TOKENS": "max_tokens",
        "SAFETY": "refusal",
        "PROHIBITED_CONTENT": "refusal",
        "BLOCKLIST": "refusal",
        "SPII": "refusal",
        "RECITATION": "refusal",
        "MALFORMED_FUNCTION_CALL": "malformed_tool_call",
    }.get(name, name.lower())


# -- Anthropic ------------------------------------------------------------


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        api_key: str | None = None,
    ) -> None:
        super().__init__(model, system_prompt, tools)

        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY") or None)
        self._messages: list[dict[str, Any]] = []

    def start(self, user_text: str) -> None:
        self._messages = [{"role": "user", "content": user_text}]

    def advance(self) -> LLMResponse:
        # Streaming keeps a long tool-heavy turn under the HTTP timeout.
        def call():
            with self._client.messages.stream(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=self.system_prompt,
                tools=self.tools,
                messages=self._messages,
                thinking={"type": "adaptive", "display": "summarized"},
            ) as stream:
                return stream.get_final_message()

        raw = _retry(call, f"anthropic {self.model}")
        self._messages.append({"role": "assistant", "content": raw.content})

        response = LLMResponse(stop_reason=raw.stop_reason or "end_turn")
        for block in raw.content:
            if block.type == "thinking":
                response.thoughts.append(block.thinking)
            elif block.type == "text":
                response.text.append(block.text)
            elif block.type == "tool_use":
                response.tool_calls.append(
                    LLMToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )

        response.input_tokens = raw.usage.input_tokens
        response.output_tokens = raw.usage.output_tokens
        return response

    def submit_tool_results(self, outcomes: list[ToolOutcome]) -> None:
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": outcome.call.id,
                        "content": outcome.text,
                        "is_error": outcome.is_error,
                    }
                    for outcome in outcomes
                ],
            }
        )


# -- factory --------------------------------------------------------------

_CLIENTS: dict[str, type[LLMClient]] = {
    "gemini": GeminiClient,
    "anthropic": AnthropicClient,
}


def infer_provider(model: str) -> str:
    lowered = model.lower()
    if lowered.startswith("gemini"):
        return "gemini"
    if lowered.startswith("claude"):
        return "anthropic"
    return DEFAULT_PROVIDER


def build_client(
    *,
    model: str | None = None,
    provider: str | None = None,
    system_prompt: str,
    tools: list[dict[str, Any]],
    api_key: str | None = None,
    requests_per_minute: int | None = None,
) -> LLMClient:
    """Pick a client from an explicit provider, or infer it from the model name."""
    if model is None:
        provider = provider or DEFAULT_PROVIDER
        model = DEFAULT_MODELS[provider]
    provider = provider or infer_provider(model)

    client_cls = _CLIENTS.get(provider)
    if client_cls is None:
        known = ", ".join(sorted(_CLIENTS))
        raise LLMError(f"Unknown LLM provider {provider!r}. Known providers: {known}.")

    kwargs: dict[str, Any] = {}
    if provider == "gemini" and requests_per_minute is not None:
        kwargs["requests_per_minute"] = requests_per_minute
    return client_cls(
        model=model, system_prompt=system_prompt, tools=tools, api_key=api_key, **kwargs
    )
