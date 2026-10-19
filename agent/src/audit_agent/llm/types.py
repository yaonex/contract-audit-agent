"""Shared LLM data types. Kept apart from client.py so cache.py / cost.py / config.py can
import them without a circular import (client imports cache; config imports CacheMode)."""
from enum import StrEnum

from pydantic import BaseModel, Field


class CacheMode(StrEnum):
    """How the client uses the response cache.

    OFF        never read or write. Default: production audits must be able to re-run, and
               experiments that change the LLM input need real random sampling.
    READWRITE  use a hit, otherwise call the API and store the answer. For development,
               iterating on matcher/post-processing, resuming a crashed evaluation.
    REPLAY     read only. A miss is an error, the API is never called and no key is needed.
               Proves the pipeline is deterministic given recorded answers.
    """

    OFF = "off"
    READWRITE = "readwrite"
    REPLAY = "replay"


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