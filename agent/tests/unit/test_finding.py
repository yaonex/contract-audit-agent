import pytest
from pydantic import ValidationError

from audit_agent.schemas.finding import (
    CodeLocation, Finding, FindingDraft, FindingSource, FindingStatus,
)


def make_draft(**over) -> FindingDraft:
    base = dict(
        category="reentrancy",
        title="Reentrancy in withdraw",
        description="External call before balance update.",
        location=CodeLocation(file="Bank.sol", line_start=10, line_end=14, function="withdraw"),
        severity="high",
        confidence=0.8,
    )
    base.update(over)
    return FindingDraft.model_validate(base)


def test_valid_draft():
    d = make_draft()
    assert d.category == "reentrancy"


def test_category_is_normalized():
    assert make_draft(category="Access Control").category == "access_control"
    assert make_draft(category="access-control").category == "access_control"


def test_unknown_category_rejected_with_helpful_message():
    with pytest.raises(ValidationError) as exc:
        make_draft(category="made_up")
    assert "must be one of" in str(exc.value)


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_confidence_range(bad):
    with pytest.raises(ValidationError):
        make_draft(confidence=bad)


def test_line_range_must_be_ordered():
    with pytest.raises(ValidationError):
        CodeLocation(file="A.sol", line_start=10, line_end=5)


def test_from_draft_sets_system_fields():
    f = Finding.from_draft(make_draft(), FindingSource.DISCOVERER)
    assert f.status == FindingStatus.CANDIDATE
    assert len(f.id) == 12
    assert f.fingerprint() == "reentrancy|Bank.sol|withdraw"


def test_draft_does_not_accept_status_from_llm():
    # Extra keys are ignored, so an LLM-supplied "status" cannot leak in.
    d = FindingDraft.model_validate({**make_draft().model_dump(), "status": "confirmed"})
    assert not hasattr(d, "status")