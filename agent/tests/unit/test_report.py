from audit_agent.schemas.finding import CodeLocation, Finding, FindingDraft, FindingSource, Severity
from audit_agent.schemas.report import AuditReport, CostSummary, RunMeta, StaticAnalysisStatus, ContextInfo


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


def test_static_analysis_defaults_to_not_run_and_roundtrips():
    report = AuditReport(
        meta=RunMeta(model="m", prompt_version="v1", tool_versions={"slither": "0.11.6"}),
        target="A.sol",
    )
    assert report.static_analysis.status == StaticAnalysisStatus.NOT_RUN
    assert AuditReport.model_validate_json(report.model_dump_json()) == report


def test_context_info_roundtrips_inside_a_report():
    info = ContextInfo(
        file="A.sol", total_lines=100, included_ranges=[(1, 40), (81, 100)],
        truncated=True, est_tokens=10, max_tokens=20_000, imports=['import "./B.sol";'],
    )
    report = AuditReport(meta=RunMeta(model="m", prompt_version="v1"), target="A.sol", context=info)
    assert AuditReport.model_validate_json(report.model_dump_json()) == report
    assert AuditReport(meta=RunMeta(model="m", prompt_version="v1"), target="A.sol").context is None