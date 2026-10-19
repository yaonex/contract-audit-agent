"""OpenAI-compatible chat client.

Scope: one call to one model. Caching and budgets are layered on in cache.py / cost.py.

Design notes:
- Two kinds of retry, never mixed:
    transport (network, 429, 5xx) -> delegated to the openai SDK via max_retries;
    semantic  (200 but invalid JSON) -> structured.py, which feeds the error back to the model.
- SDK exceptions are wrapped in LLMError so callers never import openai exceptions
  (swapping SDK or provider touches only this file).
- Tool-call arguments stay a RAW string. A model may emit truncated JSON; that is the model's
  mistake and the tool loop feeds it back for repair, so the client must not parse or fix it.
- Only whitelisted models can be used (see models.py). Model facts are exposed as
  `client.model_info` so the tool loop can pick serial/parallel execution and strip
  reasoning_content correctly.
- temperature=0 is NOT a reproducibility guarantee (servers still vary slightly).
  Reproducibility comes from the response cache (cache.py) and pinned model versions.
"""
import json
from contextlib import nullcontext
from typing import Any

import openai
from openai import OpenAI
from pydantic import BaseModel, Field

from audit_agent.config import Settings
from audit_agent.llm.models import SupportedModel, require_supported
from audit_agent.llm.cost import CostTracker
from audit_agent.observability.tracer import SpanHandle, Tracer


class LLMError(Exception):
    """Any failure talking to the model."""


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # raw JSON string from the model; parsed and validated by the tool loop


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatResult(BaseModel):
    content: str | None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    reasoning_content: str | None = None


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        tracer: Tracer | None = None,
        sdk: Any | None = None,                    # injectable for tests
        model_info: SupportedModel | None = None,  # injectable: tests and verify_model.py
        budget: CostTracker | None = None,  
    ) -> None:
        # Production path: the model must be on the whitelist, or we fail at startup.
        # Injection exists so unit tests can use fake model names and the admission script
        # can probe a model that is not registered yet. Do NOT add fake models to the whitelist.
        if model_info is None:
            model_info = require_supported(settings.llm_model)
        elif model_info.name != settings.llm_model:
            raise ValueError(
                f"model_info.name '{model_info.name}' != settings.llm_model '{settings.llm_model}'"
            )
        self.model_info = model_info
        self.model = settings.llm_model
        self._tracer = tracer
        self._budget = budget
        if sdk is None:
            sdk = OpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.get_secret_value(),
                timeout=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,  # transport retries only
            )
        self._sdk = sdk

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        # Optional params are only sent when given: some servers reject unknown/null params.
        if tools:
            kwargs["tools"] = tools
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if response_format is not None:
            kwargs["response_format"] = response_format

        # nullcontext keeps the body free of `if tracer` checks when no tracer is given.
        span = (
            self._tracer.span(
                "llm", "chat",
                temperature=temperature, n_messages=len(messages), has_tools=bool(tools),
            )
            if self._tracer
            else nullcontext(SpanHandle())
        )
        with span as sp:
            sp.set(model=self.model)
            if self._budget:
                self._budget.check()
            try:
                response = self._sdk.chat.completions.create(**kwargs)
            except openai.OpenAIError as exc:
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc

            result = self._parse(response)
            if self._budget:
                # Failed calls are not recorded (nothing returned to count). Providers may
                # still bill some failures; accepted for the MVP.
                self._budget.record(
                    result.usage,
                    prompt_chars=len(json.dumps(messages, ensure_ascii=False, default=str)),
                    completion_chars=len(result.content or "") + len(result.reasoning_content or ""),
                )
            sp.set(
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit=False,
                finish_reason=result.finish_reason,
            )
            if self._tracer:
                # Full request+response go to a blob file; the trace line stays small.
                # `kwargs` never contains the API key (it lives inside the SDK object).
                payload = {"request": kwargs, "response": result.model_dump()}
                sp.set(payload_blob=self._tracer.write_blob(
                    "llm_call", json.dumps(payload, ensure_ascii=False, default=str, indent=2)
                ))
            return result

    def _parse(self, response: Any) -> ChatResult:
        if not getattr(response, "choices", None):
            raise LLMError("response contained no choices")
        choice = response.choices[0]
        message = choice.message
        # Whitelisted models are verified to return usage, but stay defensive: a missing
        # usage must not crash here (cost.py decides how to treat zero usage).
        usage = getattr(response, "usage", None)
        return ChatResult(
            content=message.content,
            tool_calls=[
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                for tc in (message.tool_calls or [])
            ],
            finish_reason=choice.finish_reason,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
            model=getattr(response, "model", self.model),
            # Vendor extension, absent from the SDK types, hence getattr.
            reasoning_content=getattr(message, "reasoning_content", None),
        )