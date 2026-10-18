"""Whitelist of verified models.

Why a whitelist instead of runtime capability detection / fallback defaults:
- Capabilities (tool calling, usage reporting, JSON-following strength) are facts about a
  *specific model version*, and checking them is a one-off manual job, not a runtime concern.
- Unsupported features often fail SILENTLY (HTTP 200 with wrong output), so "try it and catch
  the error" does not work, and a silent fallback would make eval results incomparable.
- So: only models someone has verified can run; anything else is rejected at startup.
- Matching is EXACT, never by prefix: "qwen-plus" and "qwen-plus-2025-xx-xx" may behave
  differently, and eval requires pinning the exact version.

How to add a model: run `make verify-model MODEL=<name>`, read the report, and paste the
suggested entry below. See scripts/verify_model.py for what is checked.

Productization path: this dict becomes a DB table (with verifier, date, evidence) behind the
same require_supported() interface. For a portfolio project a constant is enough.
"""
from datetime import date

from pydantic import BaseModel


class SupportedModel(BaseModel, frozen=True):
    """Verified facts about one model. No defaults on purpose: every field must be
    filled in explicitly (and therefore looked up) when a model is registered."""

    name: str                # exact string sent to the API, including any version suffix
    parallel_tools: bool     # True: may return several tool_calls in one turn.
                             # False: tool loop executes calls strictly one at a time.
    reasoning_content: bool  # True: replies carry a separate reasoning field; the tool loop
                             # must strip it from history before sending messages back.
    verified_on: date        # when the verification was done (recorded in eval reports)


# Hard requirements for admission (checked by scripts/verify_model.py):
#   1. tool calling works, including a full tool-result round trip
#   2. responses include token usage (the cost budget depends on it)
#   3. can follow a JSON-only instruction well enough for Pydantic validation + retry
# Not requirements: json_schema strict mode (we never rely on it), json_object mode (optional).
#
# Template (paste the line printed by `make verify-model`):
#   SupportedModel(name="deepseek-flash", parallel_tools=False,
#                  reasoning_content=False, verified_on=date(2026, 10, 16)),
SUPPORTED_MODELS: dict[str, SupportedModel] = {
    m.name: m
    for m in [
        # Empty until a model has been verified with `make verify-model`.
        SupportedModel(name="deepseek-flash", parallel_tools=True, reasoning_content=True, verified_on=date(2026, 10, 18)),
    ]
}


def require_supported(model: str) -> SupportedModel:
    """Return the verified record for `model`, or fail fast at startup."""
    try:
        return SUPPORTED_MODELS[model]
    except KeyError:
        raise ValueError(
            f"model '{model}' is not in the verified list {sorted(SUPPORTED_MODELS)}. "
            f"Run `make verify-model MODEL={model}`, then add it to SUPPORTED_MODELS."
        ) from None