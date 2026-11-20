"""Run tracing, as a seam the agent loop talks to instead of a vendor SDK.

Instrumented at the `AgentLoop` level rather than inside `GeminiClient` /
`AnthropicClient`, so one implementation covers every provider and swapping
providers does not change what a trace looks like.

Three rules hold everywhere in this module:

1. **Tracing never breaks a run.** Every call into a tracer is wrapped; an
   exception is logged and swallowed, the same bargain `on_step` makes in
   `agent/loop.py`. An exception raised by the *traced work* is a different
   thing: it marks the span failed and is re-raised.
2. **Turns and tool calls are context managers.** Langfuse derives a span's
   duration from when it was opened and closed, and `start_observation` has no
   `start_time` parameter -- so a span created after the work finished would
   report ~0ms and the waterfall would be useless.
3. **Nothing secret leaves the process.** Payloads pass through `redact()` on
   the way out, and the Langfuse client is additionally given it as its `mask`
   hook, so anything the SDK serialises is scrubbed even on a path this module
   forgot about.
"""

import logging
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol

from agent.executors import truncate

logger = logging.getLogger(__name__)

# Per-field ceilings for what gets sent. Generous enough to debug a run, small
# enough that one `write_file` of a 60k-char source file cannot dominate a trace.
MAX_TRACED_CHARS = 4_000
MAX_TRACED_ARG_CHARS = 2_000

REDACTED = "[redacted]"

# Field names whose value is replaced wholesale, however it looks.
SECRET_KEY_NAMES = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|private[_-]?key|authorization)",
    re.I,
)

# Token *counts* are not credentials. Spelled out rather than loosened into the
# pattern above, because every boundary rule that lets `input_tokens` through
# also lets `api_token` through: `input_tokens` contains `token`, and without
# this exemption the usage figures tracing exists to collect arrive as
# "[redacted]".
COUNT_KEY_NAMES = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "candidates_tokens",
        "thoughts_tokens",
        "thinking_tokens",
        "visible_tokens",
        "cached_tokens",
        "max_tokens",
        "max_output_tokens",
        "token_count",
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
    }
)

# Shapes that are a credential wherever they appear, including mid-sentence in a
# command the agent tried to run or a file it tried to write.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # PEM blocks: match the whole body, not just the header.
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),           # Google / Gemini
    re.compile(r"\bsk-ant-[0-9A-Za-z_\-]{20,}"),        # Anthropic
    re.compile(r"\b(?:sk|pk|rk)-[0-9A-Za-z_\-]{16,}"),  # OpenAI, Stripe, Langfuse
    re.compile(r"\bgh[pousr]_[0-9A-Za-z]{20,}"),        # GitHub
    re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}"),     # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                # AWS access key id
    re.compile(r"\bey[JI][0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}"),  # JWT
    # `API_KEY=hunter2`, `--token: abc123`, `Bearer xyz` -- keep the name, drop the value.
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|bearer)\b([\"'\s:=]+)([^\s\"'&]{8,})"
    ),
)


def redact(value: Any, *, limit: int = MAX_TRACED_CHARS) -> Any:
    """Scrub and shorten one payload on its way to a tracer.

    Walks containers so a credential nested in tool arguments is caught too.
    Unknown types are stringified rather than handed over as objects, since the
    exporter would serialise them anyway and this keeps the limits honest.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value, limit=limit)
    # Before the Sequence branch: bytes are a Sequence of ints, and iterating
    # them would hand over a credential as a list of character codes -- scrubbed
    # by nothing, and unreadable enough that nobody would notice.
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _redact_text(bytes(value).decode("utf-8", errors="replace"), limit=limit)
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if isinstance(key, str)
                and key.lower() not in COUNT_KEY_NAMES
                and SECRET_KEY_NAMES.search(key)
                else redact(item, limit=limit)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence):
        return [redact(item, limit=limit) for item in value]
    return _redact_text(str(value), limit=limit)


def _redact_text(text: str, *, limit: int) -> str:
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 3:
            # Keep the name and separator so the trace still shows what was set.
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return truncate(text, limit)


def redact_arguments(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Tool arguments, scrubbed and held to the tighter per-argument ceiling.

    `write_file` passes a whole file as one argument; without its own limit a
    single call would crowd out everything else in the trace.
    """
    if not arguments:
        return {}
    return {str(k): v for k, v in redact(dict(arguments), limit=MAX_TRACED_ARG_CHARS).items()}


def _describe(exc: BaseException) -> str:
    """A one-line exception summary, bounded so a huge message cannot bloat a span."""
    return f"{type(exc).__name__}: {exc}"[:500]


def run_completed_score(error: str | None, stop_reason: str) -> int:
    """1 only for a run that finished of its own accord.

    Exhausting the step budget is not finishing: the agent stopped because it
    ran out of turns, which is the case worth filtering out when judging whether
    a model can do a task at all.
    """
    return 1 if error is None and stop_reason == "end_turn" else 0


# -- the seam -------------------------------------------------------------


class TurnSink(Protocol):
    """Receives what one model turn produced, once it is known."""

    def record(
        self,
        *,
        model: str | None = None,
        thoughts: Sequence[str] = (),
        text: Sequence[str] = (),
        tool_calls: Sequence[Mapping[str, Any]] = (),
        stop_reason: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None: ...


class ToolSink(Protocol):
    """Receives what one tool call returned, once it is known."""

    def record(self, *, text: str | None = None, is_error: bool = False, blocked: bool = False) -> None: ...


class Tracer(Protocol):
    """What `AgentLoop` needs from a tracer. Structural on purpose, so a test
    double does not have to inherit anything."""

    def start_run(
        self,
        *,
        run_id: str,
        description: str,
        repo_url: str,
        model: str,
        provider: str,
    ) -> None: ...

    def turn(self, *, number: int) -> AbstractContextManager[TurnSink]: ...

    def tool(self, *, name: str, arguments: Mapping[str, Any] | None) -> AbstractContextManager[ToolSink]: ...

    def end_run(
        self,
        *,
        final_message: str | None,
        stop_reason: str,
        error: str | None,
        steps_used: int,
        tool_error_count: int,
        guardrail_block_count: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> None: ...

    def flush(self) -> None: ...


class _NullSink:
    """Accepts and discards. One object serves both sink protocols."""

    def record(self, **_kwargs: Any) -> None:
        return None


class NoopTracer:
    """The default. Costs one attribute lookup and an empty `with` per turn."""

    def start_run(self, **_kwargs: Any) -> None:
        return None

    @contextmanager
    def turn(self, **_kwargs: Any) -> Iterator[TurnSink]:
        yield _NullSink()

    @contextmanager
    def tool(self, **_kwargs: Any) -> Iterator[ToolSink]:
        yield _NullSink()

    def end_run(self, **_kwargs: Any) -> None:
        return None

    def flush(self) -> None:
        return None


# -- Langfuse -------------------------------------------------------------

RUN_SPAN_NAME = "agent-run"

# One client per (key, host) per process. Celery forks workers, and each fork
# gets its own exporter thread; building a client per run would leak threads.
_clients: dict[tuple[str, str], Any] = {}


def _mask(*, data: Any, **_kwargs: Any) -> Any:
    """The SDK's own scrub hook, so even a payload this module never saw is
    redacted before it leaves the process."""
    try:
        return redact(data)
    except Exception:  # noqa: BLE001 - masking must not break the exporter
        logger.exception("masking failed; dropping the field rather than sending it")
        return REDACTED


def _get_client(public_key: str, secret_key: str, host: str | None) -> Any:
    cache_key = (public_key, host or "")
    client = _clients.get(cache_key)
    if client is None:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host or None,
            mask=_mask,
        )
        _clients[cache_key] = client
    return client


class _TurnValues:
    """Collects one turn's outcome; the context manager writes it to the span."""

    __slots__ = (
        "model",
        "thoughts",
        "text",
        "tool_calls",
        "stop_reason",
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "failure",
        "details",
    )

    def __init__(self) -> None:
        # Set when the traced work raised. The span's level is decided once, at
        # close: an earlier update would be overwritten by the closing one.
        self.failure: str | None = None
        # Whatever the provider said about a call that produced no response.
        self.details: Mapping[str, Any] | None = None
        self.model: str | None = None
        self.thoughts: Sequence[str] = ()
        self.text: Sequence[str] = ()
        self.tool_calls: Sequence[Mapping[str, Any]] = ()
        self.stop_reason: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.thinking_tokens = 0

    def record(
        self,
        *,
        model: str | None = None,
        thoughts: Sequence[str] = (),
        text: Sequence[str] = (),
        tool_calls: Sequence[Mapping[str, Any]] = (),
        stop_reason: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.thoughts = thoughts
        self.text = text
        self.tool_calls = tool_calls
        self.stop_reason = stop_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.thinking_tokens = thinking_tokens
        self.details = details


class _ToolValues:
    __slots__ = ("text", "is_error", "blocked", "failure")

    def __init__(self) -> None:
        self.failure: str | None = None
        self.text: str | None = None
        self.is_error = False
        self.blocked = False

    def record(self, *, text: str | None = None, is_error: bool = False, blocked: bool = False) -> None:
        self.text = text
        self.is_error = is_error
        self.blocked = blocked


def _turn_usage(values: "_TurnValues") -> dict[str, int]:
    """Token usage for one turn, from the response or from a failure's details.

    A call that raised has no response to count, but the provider still reported
    what it spent -- which is the whole point of capturing details: a turn that
    burned 8,000 thinking tokens and returned nothing should not read as zero.
    """
    usage = {"input": values.input_tokens, "output": values.output_tokens}
    if values.thinking_tokens:
        usage["thinking"] = values.thinking_tokens
    details = values.details or {}
    if not values.input_tokens and details.get("prompt_tokens"):
        usage["input"] = int(details["prompt_tokens"])
    if not values.output_tokens:
        spent = (details.get("candidates_tokens") or 0) + (details.get("thoughts_tokens") or 0)
        if spent:
            usage["output"] = int(spent)
    if details.get("thoughts_tokens"):
        # Broken out so thinking spend is filterable on its own in Langfuse.
        usage["thinking"] = int(details["thoughts_tokens"])
    return usage


class LangfuseTracer:
    """One Langfuse trace per agent run.

    The root span *is* the run: v5 has no separate trace object, so the trace's
    input and output are whatever the root span carries. `session_id` is the
    run id verbatim, which is what makes a run findable in the UI; the trace id
    itself has to be 32 hex characters, so it is seeded from the run id and is
    therefore stable for a given run.
    """

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        host: str | None = None,
    ) -> None:
        self._client = _get_client(public_key, secret_key, host)
        self._root: Any = None
        self._attrs: Any = None

    # -- run --------------------------------------------------------------

    def start_run(
        self,
        *,
        run_id: str,
        description: str,
        repo_url: str,
        model: str,
        provider: str,
    ) -> None:
        try:
            from langfuse import propagate_attributes

            # session_id and tags are trace-level in Langfuse, and in v5 the
            # only supported way to set them is this context manager, which
            # stamps every span opened inside it. It is entered here and exited
            # in end_run, which AgentLoop.run calls from a `finally`.
            self._attrs = propagate_attributes(
                session_id=run_id,
                trace_name=RUN_SPAN_NAME,
                tags=[f"model:{model}", f"provider:{provider}"],
            )
            self._attrs.__enter__()
            self._root = self._client.start_observation(
                trace_context={"trace_id": self._client.create_trace_id(seed=run_id)},
                name=RUN_SPAN_NAME,
                as_type="agent",
                input=redact({"description": description, "repo_url": repo_url}),
                metadata={"run_id": run_id, "model": model, "provider": provider},
            )
        except Exception:  # noqa: BLE001 - an untraced run beats a failed run
            logger.exception("could not start the Langfuse trace for run %s", run_id)
            self._root = None
            self._release_attrs()

    def end_run(
        self,
        *,
        final_message: str | None,
        stop_reason: str,
        error: str | None,
        steps_used: int,
        tool_error_count: int,
        guardrail_block_count: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> None:
        try:
            root = self._root
            if root is not None:
                root.update(
                    output=redact(
                        {
                            "final_message": final_message,
                            "stop_reason": stop_reason,
                            "error": error,
                        }
                    ),
                    metadata={
                        "steps_used": steps_used,
                        "tool_error_count": tool_error_count,
                        "guardrail_block_count": guardrail_block_count,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "thinking_tokens": thinking_tokens,
                    },
                    level="ERROR" if error else "DEFAULT",
                    status_message=redact(error) if error else None,
                )
                for name, value in (
                    ("run_completed", run_completed_score(error, stop_reason)),
                    ("steps_used", steps_used),
                    ("tool_error_count", tool_error_count),
                    ("guardrail_block_count", guardrail_block_count),
                ):
                    root.score_trace(name=name, value=value, data_type="NUMERIC")
                root.end()
        except Exception:  # noqa: BLE001 - never let bookkeeping fail a finished run
            logger.exception("could not close the Langfuse trace")
        finally:
            self._root = None
            self._release_attrs()

    def _release_attrs(self) -> None:
        """Leave the trace-attribute context exactly once.

        Set to None first: a stale context manager left on the instance would be
        exited twice and poison the OTel context for the next run in this worker.
        """
        attrs, self._attrs = self._attrs, None
        if attrs is None:
            return
        try:
            attrs.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.exception("could not release the Langfuse trace attributes")

    # -- turns and tools --------------------------------------------------

    @contextmanager
    def turn(self, *, number: int) -> Iterator[TurnSink]:
        span = self._open(name=f"turn-{number}", as_type="generation", input={"turn": number})
        values = _TurnValues()
        try:
            yield values
        except Exception as exc:
            values.failure = _describe(exc)
            raise
        finally:
            self._close_turn(span, values)

    @contextmanager
    def tool(self, *, name: str, arguments: Mapping[str, Any] | None) -> Iterator[ToolSink]:
        span = self._open(name=name, as_type="tool", input=redact_arguments(arguments))
        values = _ToolValues()
        try:
            yield values
        except Exception as exc:
            values.failure = _describe(exc)
            raise
        finally:
            self._close_tool(span, values)

    def _open(self, *, name: str, as_type: str, input: Any) -> Any:  # noqa: A002 - the SDK's own name
        root = self._root
        if root is None:
            return None
        try:
            return root.start_observation(name=name, as_type=as_type, input=input)
        except Exception:  # noqa: BLE001
            logger.exception("could not open the %s observation %r", as_type, name)
            return None

    def _close_turn(self, span: Any, values: _TurnValues) -> None:
        if span is None:
            return
        try:
            span.update(
                level="ERROR" if values.failure else "DEFAULT",
                status_message=values.failure,
                model=values.model,
                output=redact(
                    {
                        "text": list(values.text),
                        "thoughts": list(values.thoughts),
                        "tool_calls": [
                            {"name": call.get("name"), "arguments": redact_arguments(call.get("arguments"))}
                            for call in values.tool_calls
                        ],
                    }
                ),
                usage_details=_turn_usage(values),
                metadata={"stop_reason": values.stop_reason, **dict(values.details or {})},
            )
            span.end()
        except Exception:  # noqa: BLE001
            logger.exception("could not close a turn observation")

    def _close_tool(self, span: Any, values: _ToolValues) -> None:
        if span is None:
            return
        try:
            span.update(
                output=redact(values.text),
                metadata={"is_error": values.is_error, "guardrail_blocked": values.blocked},
                # Precedence matters. A dispatch that raised is an ERROR. A
                # blocked call is only a WARNING -- the guardrail doing its job
                # is not a fault, and colouring it red hides the real errors.
                level=(
                    "ERROR"
                    if values.failure
                    else "WARNING"
                    if values.blocked
                    else "ERROR"
                    if values.is_error
                    else "DEFAULT"
                ),
                status_message=values.failure,
            )
            span.end()
        except Exception:  # noqa: BLE001
            logger.exception("could not close a tool observation")

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:  # noqa: BLE001
            logger.exception("could not flush Langfuse")


# -- guards ---------------------------------------------------------------
#
# `LangfuseTracer` already swallows its own failures, but `AgentLoop` accepts any
# object satisfying the protocol. These wrappers make the promise hold for a
# tracer the loop did not write, including one that raises on `__enter__`.


def guard(label: str, call: Any, /, **kwargs: Any) -> None:
    """Invoke a tracer method; log and swallow anything it throws."""
    try:
        call(**kwargs)
    except Exception:  # noqa: BLE001 - tracing is never worth a failed run
        logger.exception("tracer %s failed", label)


class _SafeSink:
    """Passes records to a sink that may be hostile."""

    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def record(self, **kwargs: Any) -> None:
        try:
            self._inner.record(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("tracer sink rejected a record")


@contextmanager
def _guarded(label: str, open_cm: Any) -> Iterator[Any]:
    """Run a tracer's context manager without letting it affect the body.

    Its `__exit__` return value is deliberately ignored: a tracer must not be
    able to swallow a real exception from the work it is observing.
    """
    manager: Any = None
    sink: Any = _NullSink()
    try:
        manager = open_cm()
        sink = manager.__enter__()
    except Exception:  # noqa: BLE001
        logger.exception("tracer %s failed to open", label)
        manager = None
        sink = _NullSink()

    def close(exc: BaseException | None) -> None:
        nonlocal manager
        if manager is None:
            return
        current, manager = manager, None
        try:
            if exc is None:
                current.__exit__(None, None, None)
            else:
                current.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:  # noqa: BLE001
            logger.exception("tracer %s failed to close", label)

    try:
        yield _SafeSink(sink)
    except Exception as exc:
        close(exc)
        raise
    finally:
        close(None)


def guard_turn(tracer: Any, *, number: int) -> AbstractContextManager[Any]:
    return _guarded("turn", lambda: tracer.turn(number=number))


def guard_tool(tracer: Any, *, name: str, arguments: Mapping[str, Any] | None) -> AbstractContextManager[Any]:
    return _guarded("tool", lambda: tracer.tool(name=name, arguments=arguments))


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def build_tracer(
    *,
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
) -> Tracer:
    """A `LangfuseTracer` when both keys are set, a `NoopTracer` otherwise.

    `None` for an argument means read the environment, so the CLI in
    `run_agent.py` gets tracing without the settings object the worker uses.
    Importing the SDK is deferred to here: with tracing off, `langfuse` is never
    imported at all.
    """
    public_key = _env("LANGFUSE_PUBLIC_KEY") if public_key is None else public_key.strip()
    secret_key = _env("LANGFUSE_SECRET_KEY") if secret_key is None else secret_key.strip()
    host = _env("LANGFUSE_HOST") if host is None else host.strip()

    if not public_key or not secret_key:
        return NoopTracer()
    try:
        return LangfuseTracer(public_key=public_key, secret_key=secret_key, host=host or None)
    except Exception:  # noqa: BLE001 - tracing is optional; the run is not
        logger.warning("Langfuse tracing unavailable; continuing untraced", exc_info=True)
        return NoopTracer()


__all__ = [
    "COUNT_KEY_NAMES",
    "MAX_TRACED_ARG_CHARS",
    "MAX_TRACED_CHARS",
    "REDACTED",
    "RUN_SPAN_NAME",
    "LangfuseTracer",
    "NoopTracer",
    "ToolSink",
    "Tracer",
    "TurnSink",
    "build_tracer",
    "guard",
    "guard_tool",
    "guard_turn",
    "redact",
    "redact_arguments",
    "run_completed_score",
]
