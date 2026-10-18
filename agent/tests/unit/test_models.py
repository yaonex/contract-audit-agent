from datetime import date

import pytest
from pydantic import ValidationError

from audit_agent.llm.models import SUPPORTED_MODELS, SupportedModel, require_supported


def make_model(name="m1") -> SupportedModel:
    return SupportedModel(
        name=name, parallel_tools=False, reasoning_content=False, verified_on=date(2026, 1, 1)
    )


def test_unknown_model_rejected_with_actionable_message():
    with pytest.raises(ValueError, match="make verify-model"):
        require_supported("definitely-not-registered")


def test_known_model_returned_and_match_is_exact(monkeypatch):
    monkeypatch.setitem(SUPPORTED_MODELS, "m1", make_model("m1"))
    assert require_supported("m1").name == "m1"
    with pytest.raises(ValueError):
        require_supported("m1-2025-01-01")  # no prefix matching: versions are registered separately


def test_entries_are_immutable():
    m = make_model()
    with pytest.raises(ValidationError):
        m.parallel_tools = True  # type: ignore[misc]


def test_registry_keys_match_names():
    # Guards against copy-paste mistakes when a new entry is added.
    for key, model in SUPPORTED_MODELS.items():
        assert key == model.name