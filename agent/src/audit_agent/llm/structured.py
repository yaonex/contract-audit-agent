"""Structured output: ask for JSON, validate with Pydantic, retry with feedback.

Why we do not rely on vendor "strict JSON schema" modes: models are swappable and support
varies (and often fails silently). Pydantic validation + retry works for every model.

Pipeline for one reply:
  1. iter_candidates() lazily yields JSON objects found in the text. It only PRODUCES
     candidates; it never decides what is correct.
  2. structured_chat() validates candidates one by one and returns the FIRST valid one.
     The Pydantic schema is the final judge: a Solidity snippet can never pass it.

Extraction rules:
- Sources, in priority order: code blocks labelled `json` or unlabelled, then the whole text.
  Blocks labelled with another language (solidity, python, ...) are excluded from both.
- Inside a source, every '{' is tried. A failed parse moves on to the next '{' (prose like
  "the struct {a, b}" before the real JSON must not hide it). A successful parse jumps past
  the end of that object, so nested sub-objects are never reported as separate candidates.
- Parsing uses json.JSONDecoder.raw_decode: a real grammar-aware parser that stops after one
  complete value. Regex or brace counting would break on braces inside strings.
- Empty objects are dropped: an empty Solidity body `function f() {}` is valid JSON `{}`, and
  no schema of ours accepts an empty object.
- No <answer> tags (not worth a third layer yet). `layer` is recorded in the trace; add tags
  only if the data shows many replies falling through to the "text" layer.
- If a reply contains several different valid objects, the first wins (the prompt must ask
  for exactly one). `extra_candidates` in the trace shows how often that happens.
- JSON with comments / trailing commas / single quotes fails to parse and is retried with the
  parser's message. We do NOT strip comments ourselves: "//" also appears inside strings
  (URLs, Solidity in `evidence`) and a regex would silently corrupt them. The failure reason
  is traced so we can measure how often it happens before adding a lenient parser.

Two failures, two different remedies (never mixed):
- no usable / invalid object -> tell the model what is wrong (error text is part of the prompt);
- finish_reason=length       -> the answer was cut off. "Your JSON is invalid" would make the
  model rewrite the same long answer and hit the same limit. Keep the messages and raise
  max_tokens. (Reasoning models spend max_tokens on thinking first.)

Retries and the response cache: every retry changes the request (messages for feedback,
max_tokens for truncation), hence the cache key, so a retry can never be served the previous
bad answer. In replay mode the same retry sequence replays exactly.

Not caught on purpose: LLMError (transport retries belong to the SDK), BudgetExceeded and
ReplayMiss (retrying cannot help). Callers must not sum ChatResult.usage; spend is whatever
the shared CostTracker says.

Output-contract note (Discoverer, Phase 1.2): use a wrapper `{"findings": [...]}` whose
`findings` field has NO default. Then `{"findings": []}` is an explicit "nothing found",
while a broken or incomplete reply fails validation instead of reading as "no vulnerabilities".
Prompt rule: never put comments in example JSON; models copy them.
"""
import json
import re
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from audit_agent.llm.types import ChatResult
from audit_agent.observability.tracer import SpanHandle, Tracer

_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
_ANSWER_LANGS = frozenset({"", "json"})   # code blocks that may hold the answer
_MAX_FEEDBACK_ERRORS = 10
_MAX_FEEDBACK_CHARS = 1500


class ChatClient(Protocol):
    """What structured_chat needs from a client (LLMClient satisfies it; tests fake it)."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = ...,
        max_tokens: int | None = ...,
    ) -> ChatResult: ...


class StructuredOutputError(Exception):
    """The model never produced a valid object within the attempt limit."""

    def __init__(self, message: str, *, attempts: int, last_content: str | None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_content = last_content


class OutputTruncated(StructuredOutputError):
    """The reply kept hitting max_tokens (up to the cap)."""


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    attempts: int
    layer: str   # which extraction layer produced the object: "fenced" | "text"


@dataclass(frozen=True)
class Candidate:
    obj: dict[str, Any]
    layer: str


@dataclass
class ScanReport:
    """Filled in while scanning; explains why nothing was found."""

    parse_error: str | None = None
    _depth: int = -1

    def note(self, exc: json.JSONDecodeError, start: int) -> None:
        # Keep the error of the attempt that got furthest: it is the one most likely to be
        # the model's real (broken) answer, not prose that merely contains a '{'.
        depth = exc.pos - start
        if depth > self._depth:
            self._depth = depth
            self.parse_error = f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"


def _is_answer_block(lang: str) -> bool:
    return lang.strip().lower() in _ANSWER_LANGS


def _scan(text: str, layer: str, seen: set[str], report: ScanReport) -> Iterator[Candidate]:
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError as exc:
            report.note(exc, pos)
            pos = text.find("{", pos + 1)       # not an object here, try the next '{'
            continue
        if isinstance(obj, dict) and obj:        # drop `{}` (e.g. an empty Solidity body)
            key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
            if key not in seen:                  # same object in a code block and in the text
                seen.add(key)
                yield Candidate(obj, layer)
        pos = text.find("{", end)                # jump past it: no nested sub-objects


def iter_candidates(text: str, report: ScanReport | None = None) -> Iterator[Candidate]:
    """Lazily yield JSON-object candidates, most likely first."""
    report = report if report is not None else ScanReport()
    seen: set[str] = set()
    for match in _FENCE.finditer(text):
        if _is_answer_block(match.group(1)):
            yield from _scan(match.group(2), "fenced", seen, report)
    # Whole text, minus code blocks of other languages (their braces are not our answer).
    remainder = _FENCE.sub(lambda m: m.group(0) if _is_answer_block(m.group(1)) else "", text)
    yield from _scan(remainder, "text", seen, report)


def format_validation_errors(exc: ValidationError) -> str:
    """Compact 'field: reason' lines. Inputs are omitted: the model already saw its own reply."""
    errors = exc.errors(include_input=False, include_url=False)
    lines = [
        f"- {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in errors[:_MAX_FEEDBACK_ERRORS]
    ]
    if len(errors) > _MAX_FEEDBACK_ERRORS:
        lines.append(f"- ... and {len(errors) - _MAX_FEEDBACK_ERRORS} more")
    return "\n".join(lines)[:_MAX_FEEDBACK_CHARS]


def _feedback_turns(previous: ChatResult, problem: str) -> list[dict[str, Any]]:
    # Only `content` goes back: reasoning_content must never be replayed into history.
    return [
        {"role": "assistant", "content": previous.content or "(empty reply)"},
        {
            "role": "user",
            "content": (
                "Your previous reply could not be used.\n"
                f"Problems:\n{problem}\n\n"
                "Reply again with ONE corrected JSON object only: no explanation, no code fences."
            ),
        },
    ]


def structured_chat[T: BaseModel](
    client: ChatClient,
    messages: list[dict[str, Any]],
    schema: type[T],
    *,
    max_attempts: int = 3,
    max_tokens: int = 4096,
    max_tokens_cap: int = 16384,
    temperature: float = 0.0,
    tracer: Tracer | None = None,
) -> StructuredResult[T]:
    """Get one validated `schema` instance from the model, retrying with feedback.

    `max_attempts` counts every model call (feedback retries and truncation retries alike).
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if max_tokens > max_tokens_cap:
        raise ValueError("max_tokens must be <= max_tokens_cap")

    base = list(messages)          # never mutate the caller's list
    convo = base
    tokens = max_tokens
    last_problem = ""
    last_content: str | None = None
    last_truncated = False

    span = (
        tracer.span("structured", schema.__name__, max_attempts=max_attempts)
        if tracer
        else nullcontext(SpanHandle())
    )
    with span as sp:
        for attempt in range(1, max_attempts + 1):
            sp.set(attempts=attempt)
            result = client.chat(convo, temperature=temperature, max_tokens=tokens)
            last_content = result.content

            if result.finish_reason == "length":
                last_truncated = True
                last_problem = f"reply cut off at max_tokens={tokens}"
                sp.set(last_problem=last_problem)
                if tokens >= max_tokens_cap:
                    raise OutputTruncated(
                        f"{last_problem}; already at the cap ({max_tokens_cap})",
                        attempts=attempt, last_content=last_content,
                    )
                tokens = min(tokens * 2, max_tokens_cap)   # same messages, bigger budget
                continue
            last_truncated = False

            text = result.content or ""
            report = ScanReport()
            candidates = iter_candidates(text, report)
            first_error: ValidationError | None = None
            checked = 0
            for cand in candidates:
                checked += 1
                try:
                    value = schema.model_validate(cand.obj)
                except ValidationError as exc:
                    if first_error is None:
                        first_error = exc
                    continue
                # Observability only: how many more candidates the reply held. Does not
                # affect the result. A frequently non-zero value means the prompt is loose.
                extra = sum(1 for _ in candidates)
                sp.set(layer=cand.layer, candidates_checked=checked, extra_candidates=extra)
                return StructuredResult(value, attempt, cand.layer)

            if first_error is not None:
                last_problem = format_validation_errors(first_error)
            elif not text.strip():
                last_problem = "the reply was empty"
            else:
                last_problem = report.parse_error or "no JSON object found in the reply"
            sp.set(last_problem=last_problem, candidates_checked=checked)
            convo = [*base, *_feedback_turns(result, last_problem)]

        error_cls = OutputTruncated if last_truncated else StructuredOutputError
        raise error_cls(
            f"no valid {schema.__name__} after {max_attempts} attempts; last problem: {last_problem}",
            attempts=max_attempts, last_content=last_content,
        )