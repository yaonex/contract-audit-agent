"""Tool-calling agent loop (our own, ~100 lines of logic; no LangChain AgentExecutor).

Why our own: we need precise control over budget, cache, trace and model quirks, and the core
loop is small. LangGraph (outer layer) orchestrates agents; this is the inner loop of ONE agent.

The loop:
    while True:
        stop if the time limit is reached
        reply = client.chat(messages, tools)             # cache / budget / trace live in client
        finish_reason == "length"  -> same messages, bigger max_tokens, no tool is run
        reply has tool_calls       -> run them, append assistant + tool messages, go on
                                      (more than max_tool_rounds rounds -> stop MAX_ROUNDS)
        no tool_calls              -> final answer; if a schema is given, validate it and, on
                                      failure, append feedback and go on (same conversation)

Rules worth knowing:
- Every tool_call_id gets a tool message, always: some OpenAI-compatible servers reject a
  history with unanswered tool calls (my recollection, not checked against docs). Failures
  (unknown tool, bad arguments, crash, skipped by the time limit) are reported to the model.
- A "round" is one model reply that contains tool calls. max_tool_rounds=N lets N rounds run;
  if reply N+1 still asks for tools we stop WITHOUT running them and without appending that
  reply, so the transcript always ends in a valid state. No "wrap-up call": how servers react
  to history containing tool calls but no `tools` is unknown, so the caller decides what a
  MAX_ROUNDS result means (the Verifier will map it to `unverified`).
- Several tool calls in one reply are ALL executed, strictly one after another. Parallel
  execution would need threads (contextvars do not propagate, breaking trace parents) and
  parallelism mostly comes from LangGraph fan-out. SupportedModel.parallel_tools therefore
  has no effect on this loop yet.
- The time limit can only be checked BETWEEN calls; a running model call or tool is bounded
  by its own timeout.
- Format retries of the final answer (schema given) are counted separately and never use up
  tool rounds. They accumulate in the history (capped by max_format_retries).
- Errors that retrying cannot fix (LLMError, BudgetExceeded, ReplayMiss, StructuredOutputError,
  OutputTruncated) propagate, but the transcript is still written to a trace blob.
- Unparseable tool arguments are NOT replayed into the history (replaced by "{}"): a bad
  payload in history could make the next request fail.
- Every model call goes through the response cache, whose key includes the messages, so tool
  output must be deterministic (see tools/registry.py) or replay mode will miss.
"""
import json
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel

from audit_agent.config import Settings
from audit_agent.llm.structured import (
    OutputTruncated,
    ParseSuccess,
    StructuredOutputError,
    feedback_user_message,
    parse_reply,
)
from audit_agent.llm.types import ChatResult, ToolCall
from audit_agent.observability.tracer import SpanHandle, Tracer
from audit_agent.tools.registry import (
    ArgumentsNotJson,
    ToolRegistry,
    ToolResult,
    parse_arguments,
)

# Whether the model's reasoning_content is sent back in later turns. All of that policy lives
# in assistant_message() below. UNVERIFIED ASSUMPTION: we strip it. Some reasoning models may
# require it between tool calls of one chain; to be checked against the provider docs and with
# a two-round tool chain in scripts/verify_model.py. If so, change it here and nowhere else.
PASS_BACK_REASONING = False

_ARGS_PREVIEW_CHARS = 300


class AgentClient(Protocol):
    """What the loop needs from a client (LLMClient satisfies it; tests fake it)."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = ...,
        temperature: float = ...,
        max_tokens: int | None = ...,
    ) -> ChatResult: ...


class StopReason(StrEnum):
    FINAL = "final"            # the model answered without asking for tools
    MAX_ROUNDS = "max_rounds"  # it still wanted tools after max_tool_rounds rounds
    TIMEOUT = "timeout"        # the total time limit was reached


@dataclass(frozen=True)
class ToolCallRecord:
    round_no: int
    tool_call_id: str
    name: str
    arguments: str
    error_kind: str | None
    output_chars: int          # size BEFORE clipping


@dataclass(frozen=True)
class ToolArtifact:
    tool: str
    value: Any


@dataclass(frozen=True)
class AgentResult[T: BaseModel]:
    stop_reason: StopReason
    content: str | None        # text of the last model reply
    value: T | None            # validated final answer (only when a schema was given and FINAL)
    messages: list[dict[str, Any]]
    artifacts: list[ToolArtifact]
    tool_calls: list[ToolCallRecord]
    rounds: int
    model_calls: int
    elapsed_s: float
    format_retries: int = 0
    truncations: int = 0


@dataclass
class _RunState:
    messages: list[dict[str, Any]]
    artifacts: list[ToolArtifact] = field(default_factory=list)
    records: list[ToolCallRecord] = field(default_factory=list)
    rounds: int = 0
    model_calls: int = 0
    format_retries: int = 0
    truncations: int = 0
    last_content: str | None = None


def assistant_message(
    reply: ChatResult, tool_calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The ONE place that turns a model reply into a history message."""
    if tool_calls:
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": reply.content or None,
            "tool_calls": tool_calls,
        }
    else:
        msg = {"role": "assistant", "content": reply.content or "(empty reply)"}
    if PASS_BACK_REASONING and reply.reasoning_content:
        msg["reasoning_content"] = reply.reasoning_content
    return msg


def _history_arguments(raw: str) -> str:
    try:
        parse_arguments(raw)
    except ArgumentsNotJson:
        return "{}"
    return raw if raw.strip() else "{}"


def _clip(text: str, limit: int) -> str:
    """Last gate for tool output. Keep head AND tail: errors usually sit at the end."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    return f"{text[:head]}\n...[truncated {len(text) - limit} chars]...\n{text[-tail:]}"


def _preview(text: str, limit: int = _ARGS_PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


class Agent:
    def __init__(
        self,
        client: AgentClient,
        registry: ToolRegistry,
        *,
        name: str = "agent",
        max_tool_rounds: int = 8,
        max_format_retries: int = 2,
        timeout_s: float = 300.0,
        max_tokens: int = 4096,
        max_tokens_cap: int = 16384,
        temperature: float = 0.0,
        tool_output_max_chars: int = 8000,
        tracer: Tracer | None = None,
        clock: Callable[[], float] = time.monotonic,   # injectable: deterministic timeout tests
    ) -> None:
        if max_tool_rounds < 0 or max_format_retries < 0 or timeout_s < 0:
            raise ValueError("max_tool_rounds, max_format_retries and timeout_s must be >= 0")
        if max_tokens > max_tokens_cap:
            raise ValueError("max_tokens must be <= max_tokens_cap")
        self._client = client
        self._registry = registry
        self._name = name
        self.max_tool_rounds = max_tool_rounds
        self._max_format_retries = max_format_retries
        self.timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._max_tokens_cap = max_tokens_cap
        self._temperature = temperature
        self._tool_output_max_chars = tool_output_max_chars
        self._tracer = tracer
        self._clock = clock

    @classmethod
    def from_settings(
        cls, client: AgentClient, registry: ToolRegistry, settings: Settings, **overrides: Any
    ) -> "Agent":
        params: dict[str, Any] = {
            "max_tool_rounds": settings.max_tool_rounds,
            "timeout_s": settings.agent_timeout_s,
        }
        params.update(overrides)
        return cls(client, registry, **params)

    def run[T: BaseModel](
        self, messages: list[dict[str, Any]], *, schema: type[T] | None = None
    ) -> AgentResult[T]:
        """Run the loop. With `schema`, the final answer is validated into a `schema` instance."""
        state = _RunState(messages=list(messages))    # never mutate the caller's list
        started = self._clock()
        deadline = started + self.timeout_s
        span = (
            self._tracer.span(
                "agent", self._name,
                max_tool_rounds=self.max_tool_rounds, structured=schema is not None,
            )
            if self._tracer
            else nullcontext(SpanHandle())
        )
        with span as sp:
            try:
                stop, content, value = self._loop(state, schema, deadline, sp)
            finally:
                # Also on exceptions: the transcript shows where the agent got to.
                sp.set(
                    model_calls=state.model_calls,
                    rounds=state.rounds,
                    tool_calls=len(state.records),
                )
                if self._tracer:
                    sp.set(transcript_blob=self._tracer.write_blob(
                        "agent_transcript",
                        json.dumps(state.messages, ensure_ascii=False, default=str, indent=2),
                    ))
            sp.set(stop_reason=stop.value)
            return AgentResult(
                stop_reason=stop,
                content=content,
                value=value,
                messages=state.messages,
                artifacts=state.artifacts,
                tool_calls=state.records,
                rounds=state.rounds,
                model_calls=state.model_calls,
                elapsed_s=self._clock() - started,
                format_retries=state.format_retries,
                truncations=state.truncations,
            )

    def _loop[T: BaseModel](
        self, state: _RunState, schema: type[T] | None, deadline: float, sp: SpanHandle
    ) -> tuple[StopReason, str | None, T | None]:
        tokens = self._max_tokens
        tools = self._registry.specs() or None
        while True:
            if self._clock() >= deadline:
                return StopReason.TIMEOUT, state.last_content, None

            reply = self._client.chat(
                state.messages, tools=tools, temperature=self._temperature, max_tokens=tokens
            )
            state.model_calls += 1
            state.last_content = reply.content

            if reply.finish_reason == "length":
                state.truncations += 1
                # Cut off (tool arguments or answer may be incomplete): run nothing, keep the
                # messages, give the model more room. Same remedy as structured_chat.
                if tokens >= self._max_tokens_cap:
                    raise OutputTruncated(
                        f"reply cut off at max_tokens={tokens}; "
                        f"already at the cap ({self._max_tokens_cap})",
                        attempts=state.model_calls, last_content=reply.content,
                    )
                tokens = min(tokens * 2, self._max_tokens_cap)
                continue

            if reply.tool_calls:
                if state.rounds >= self.max_tool_rounds:
                    sp.set(pending_tool_calls=[c.name for c in reply.tool_calls])
                    return StopReason.MAX_ROUNDS, reply.content, None
                state.rounds += 1
                self._run_round(state, reply, deadline)
                continue    # the time limit is re-checked at the top

            # No tool calls: this is the final answer.
            if schema is None:
                state.messages.append(assistant_message(reply))
                return StopReason.FINAL, reply.content, None

            parsed = parse_reply(reply.content or "", schema)
            if isinstance(parsed, ParseSuccess):
                state.messages.append(assistant_message(reply))
                sp.set(
                    layer=parsed.layer,
                    candidates_checked=parsed.candidates_checked,
                    extra_candidates=parsed.extra_candidates,
                )
                return StopReason.FINAL, reply.content, parsed.value

            sp.set(last_problem=parsed.problem)
            state.format_retries += 1
            if state.format_retries > self._max_format_retries:
                raise StructuredOutputError(
                    f"no valid {schema.__name__} after {state.format_retries} tries; "
                    f"last problem: {parsed.problem}",
                    attempts=state.model_calls, last_content=reply.content,
                )
            # Same conversation: the model keeps its whole tool history while it fixes the answer.
            state.messages.append(assistant_message(reply))
            state.messages.append(feedback_user_message(parsed.problem))

    def _run_round(self, state: _RunState, reply: ChatResult, deadline: float) -> None:
        # Run every call first; only then write the messages. An infrastructure error in the
        # middle therefore leaves the transcript at the last consistent state.
        outcomes: list[tuple[ToolCall, ToolResult]] = []
        for call in reply.tool_calls:
            if self._clock() >= deadline:
                outcomes.append((call, ToolResult(
                    "error: not executed, the time limit was reached", error_kind="skipped"
                )))
                continue
            outcomes.append((call, self._run_tool(call)))

        state.messages.append(assistant_message(reply, [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": _history_arguments(call.arguments)},
            }
            for call, _ in outcomes
        ]))
        for call, result in outcomes:
            state.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": _clip(result.content, self._tool_output_max_chars),
            })
            state.records.append(ToolCallRecord(
                round_no=state.rounds,
                tool_call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                error_kind=result.error_kind,
                output_chars=len(result.content),
            ))
            if result.artifact is not None:
                state.artifacts.append(ToolArtifact(call.name, result.artifact))

    def _run_tool(self, call: ToolCall) -> ToolResult:
        span = (
            self._tracer.span(
                "tool", call.name, tool_call_id=call.id, args=_preview(call.arguments)
            )
            if self._tracer
            else nullcontext(SpanHandle())
        )
        with span as sp:
            result = self._registry.execute(call.name, call.arguments)   # may raise INFRA errors
            sp.set(output_chars=len(result.content))
            if result.error_kind:
                sp.set(error_kind=result.error_kind)
            if result.error_kind == "tool_crashed":
                sp.fail(result.detail or "tool crashed")     # our bug: visible as an error span
            if self._tracer and len(result.content) > self._tool_output_max_chars:
                sp.set(output_blob=self._tracer.write_blob(f"tool_{call.name}", result.content))
            return result