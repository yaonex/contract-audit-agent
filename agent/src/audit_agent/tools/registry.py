"""Tool definitions and dispatch for the tool-calling loop.

A Tool = name + description + a Pydantic params model + a function. The params model is the
single source of truth: it generates the JSON schema sent to the LLM AND validates the
arguments the LLM sends back.

Contract of ToolRegistry.execute() (the loop depends on it):
- It ALWAYS returns a ToolResult for anything the model can cause or fix: unknown tool,
  arguments that are not a JSON object, arguments failing validation, a failure the tool
  reports (raise ToolError), and even a crashing tool. It never raises for those.
- It re-raises infrastructure errors (LLMError, BudgetExceeded, ReplayMiss): retrying them
  cannot help and swallowing them would hide a spent budget.
- A crashed tool's text for the model contains only the exception TYPE (no message, path or
  stack); the full text goes in ToolResult.detail for the trace.

Tool results carry two things on purpose: `content` (text the model sees) and `artifact` (a
structured object only our code sees, e.g. a PocAttempt). Never parse model-visible text back.

Rules for tool authors:
- Keep parameters FLAT (str / int / float / bool / Literal / Optional). Nested models and Enums
  make Pydantic emit `$defs`/`$ref`, which some OpenAI-compatible servers handle badly; this is
  from experience, not checked against any docs, and registration rejects them. Use `Literal`
  for choices.
- Put per-argument help in Field(description=...); it ends up in the schema the model reads.
- Keep `content` short. The loop clips it again as a last gate, but a tool knows best what
  matters (e.g. forge: keep the errors, write the full log to a blob and return its path).
- `content` must be DETERMINISTIC: it becomes part of the next request and therefore of the
  response-cache key. Timings, temp-dir paths or gas numbers make replay mode miss forever.
- Order matters: specs() follows registration order and is part of the cache key too. Never
  build the registry from a set.
"""
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from audit_agent.llm.cache import ReplayMiss
from audit_agent.llm.client import LLMError
from audit_agent.llm.cost import BudgetExceeded
from audit_agent.llm.structured import format_validation_errors

# Failures that must always propagate. Retrying can't fix them, and swallowing them would
# let an agent keep spending money or hide a broken replay.
INFRA_ERRORS = (LLMError, BudgetExceeded, ReplayMiss)

# Name rule is my recollection of the OpenAI-style limit (letters, digits, _ and -, <= 64).
# Verify against the provider's docs if a server rejects a name.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ToolError(Exception):
    """Raise from a tool for a failure the MODEL should know about (file not found, ...).
    The message is sent to the model verbatim, so keep it short and free of secrets."""


class ArgumentsNotJson(ValueError):
    """The model's tool arguments are not a JSON object."""


@dataclass(frozen=True)
class ToolResult:
    content: str                       # what the model sees
    artifact: Any = None               # what our code keeps (never shown to the model)
    error_kind: str | None = None      # None = success; else unknown_tool | bad_json |
                                       # bad_arguments | tool_failed | tool_crashed | skipped
    detail: str | None = None          # full internal error text, for the trace only

    @property
    def is_error(self) -> bool:
        return self.error_kind is not None


@dataclass(frozen=True)
class Tool[P: BaseModel]:
    name: str
    description: str
    params: type[P]
    func: Callable[[P], ToolResult | str]

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid tool name {self.name!r}: use 1-64 of A-Z a-z 0-9 _ -")
        if not self.description.strip():
            raise ValueError(f"tool '{self.name}' needs a description (the model reads it)")
        schema = self.json_schema()
        if "$defs" in schema or "$ref" in json.dumps(schema):
            raise ValueError(
                f"tool '{self.name}': parameters must be flat (no nested models or Enums); "
                "use Literal for choices"
            )

    def json_schema(self) -> dict[str, Any]:
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        return schema

    def spec(self) -> dict[str, Any]:
        """The entry for the `tools=` argument of a chat completion request."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }


def parse_arguments(raw: str) -> dict[str, Any]:
    """Parse a model's tool arguments. Empty text means "no arguments" (some models send an
    empty string for tools without parameters)."""
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArgumentsNotJson(f"{exc.msg} (line {exc.lineno}, column {exc.colno})") from exc
    if not isinstance(value, dict):
        raise ArgumentsNotJson(f"expected a JSON object, got {type(value).__name__}")
    return value


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool[Any]] = ()) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name '{tool.name}'")
        self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def execute(self, name: str, raw_arguments: str) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(sorted(self._tools)) or "(none)"
            return ToolResult(
                f"error: unknown tool '{name}'. Available tools: {available}",
                error_kind="unknown_tool",
            )
        try:
            arguments = parse_arguments(raw_arguments)
        except ArgumentsNotJson as exc:
            return ToolResult(
                f"error: arguments for '{name}' are not a valid JSON object ({exc}). "
                "Send one JSON object that matches the tool's parameter schema.",
                error_kind="bad_json",
            )
        try:
            params = tool.params.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                f"error: invalid arguments for '{name}':\n{format_validation_errors(exc)}",
                error_kind="bad_arguments",
            )
        try:
            out = tool.func(params)
        except INFRA_ERRORS:
            raise
        except ToolError as exc:
            return ToolResult(f"error: {exc}", error_kind="tool_failed")
        except Exception as exc:  # a bug in the tool: hide the details from the model
            return ToolResult(
                f"error: internal failure while running '{name}' ({type(exc).__name__}). "
                "This is not caused by your arguments; try a different approach.",
                error_kind="tool_crashed",
                detail=f"{type(exc).__name__}: {exc}",
            )
        return out if isinstance(out, ToolResult) else ToolResult(out)