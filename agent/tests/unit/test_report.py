from audit_agent.schemas.finding import CodeLocation, Finding, FindingDraft, FindingSource, Severity
from audit_agent.schemas.report import AuditReport, CostSummary, RunMeta


def make_finding() -> Finding:
    draft = FindingDraft(
        category="reentrancy", title="t", description="d",
        location=CodeLocation(file="A.sol", line_start=1, line_end=2),
        severity=Severity.HIGH, confidence=0.5,
    )
    return Finding.from_draft(draft, FindingSource.DISCOVERER)


def test_total_tokens_is_derived():
    assert CostSummary(prompt_tokens=10, completion_tokens=5).total_tokens == 15


def test_counts_by_status():
    report = AuditReport(
        meta=RunMeta(model="m", prompt_version="v1"),
        target="A.sol",
        findings=[make_finding(), make_finding()],
    )
    assert report.counts_by_status() == {"candidate": 2}


def test_json_roundtrip():
    report = AuditReport(
        meta=RunMeta(model="m", prompt_version="v1"), target="A.sol", findings=[make_finding()]
    )
    restored = AuditReport.model_validate_json(report.model_dump_json())
    assert restored == report