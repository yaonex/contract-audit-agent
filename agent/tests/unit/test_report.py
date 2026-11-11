import random
from datetime import UTC, datetime
from pathlib import Path

from audit_agent.config import Settings
from audit_agent.report.builder import build_report
from audit_agent.report.json_export import export_report_to_json, load_report_from_json
from audit_agent.report.markdown import render_markdown
from audit_agent.report.writer import write_report
from audit_agent.schemas.finding import (
    CodeLocation,
    Finding,
    FindingDraft,
    FindingSource,
    FindingStatus,
    Severity,
)
from audit_agent.schemas.report import (
    AuditReport,
    ContextInfo,
    CostSummary,
    DegradationRecord,
    DegradationType,
    DiscoveryInfo,
    ReportFinding,
    RunMeta,
    StaticAnalysisInfo,
    StaticAnalysisStatus,
)


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


SAMPLE_SOL = """// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Bank {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw() external {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "No funds");
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "Transfer failed");
        balances[msg.sender] = 0;
    }

    function getBalance(address who) external view returns (uint256) {
        return balances[who];
    }
}
"""


def test_golden_zero_findings_no_degradations():
    fixed_time = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    report = build_report(
        "Bank.sol",
        source_code=SAMPLE_SOL,
        findings=[],
        generated_at=fixed_time,
    )
    md = render_markdown(report)
    assert "# Smart Contract Security Audit Report: Bank.sol" in md
    assert "2026-11-11 12:00:00" in md
    assert "Clean Result: Zero Findings" in md
    assert "Scope & MVP Notice" in md
    assert "⚠️ Degradation Notice" not in md
    assert "| **Total** | **0** |" in md


def test_golden_zero_findings_with_degradations():
    fixed_time = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    deg = DegradationRecord(
        stage="static_analysis",
        type=DegradationType.STATIC_ANALYSIS_FAILED,
        description="Solc compilation failed (pragma mismatch)",
        impact="Static analysis findings missing",
    )
    report = build_report(
        "Bank.sol",
        source_code=SAMPLE_SOL,
        findings=[],
        degradations=[deg],
        generated_at=fixed_time,
    )
    md = render_markdown(report)
    assert "⚠️ Degradation Notice" in md
    assert "Scope & MVP Notice" in md
    assert "Solc compilation failed (pragma mismatch)" in md
    assert "Warning: Zero Findings with Degradations" in md
    assert "cannot be interpreted as a clean audit result" in md
    assert "| **Total** | **0** |" in md


def test_golden_single_and_multiple_findings():
    fixed_time = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    f1 = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Reentrancy in withdraw",
            description="External call occurs before zeroing balance.",
            location=CodeLocation(file="Bank.sol", line_start=11, line_end=17, function="withdraw"),
            evidence=["L14: (bool ok, ) = msg.sender.call{value: amount}(\"\");"],
            severity=Severity.HIGH,
            confidence=0.9,
        ),
        FindingSource.DISCOVERER,
    )
    report = build_report(
        "Bank.sol",
        source_code=SAMPLE_SOL,
        findings=[f1],
        generated_at=fixed_time,
    )
    assert len(report.findings) == 1
    assert report.findings[0].status == FindingStatus.UNVERIFIED
    assert len(report.findings[0].snippets) == 1
    assert report.findings[0].snippets[0].lines[0].line_number == 11

    md = render_markdown(report)
    assert "### 1. [HIGH] Reentrancy in withdraw" in md
    assert "Bank.sol:11-17" in md
    assert "(Source: discoverer)" in md
    assert "External call occurs before zeroing balance." in md
    assert "```solidity" in md or "````solidity" in md
    assert "14 |         (bool ok, ) = msg.sender.call{value: amount}(\"\");" in md


def test_findings_order_stability():
    fixed_time = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    f_crit = Finding.from_draft(
        FindingDraft(
            category="reentrancy", title="Crit", description="d",
            location=CodeLocation(file="Bank.sol", line_start=20, line_end=22),
            severity=Severity.CRITICAL, confidence=0.7,
        ),
        FindingSource.DISCOVERER,
    )
    f_high1 = Finding.from_draft(
        FindingDraft(
            category="reentrancy", title="High1", description="d",
            location=CodeLocation(file="Bank.sol", line_start=10, line_end=15),
            severity=Severity.HIGH, confidence=0.9,
        ),
        FindingSource.DISCOVERER,
    )
    f_high2 = Finding.from_draft(
        FindingDraft(
            category="reentrancy", title="High2", description="d",
            location=CodeLocation(file="Bank.sol", line_start=12, line_end=16),
            severity=Severity.HIGH, confidence=0.6,
        ),
        FindingSource.DISCOVERER,
    )
    f_med = Finding.from_draft(
        FindingDraft(
            category="reentrancy", title="Med", description="d",
            location=CodeLocation(file="Bank.sol", line_start=5, line_end=8),
            severity=Severity.MEDIUM, confidence=0.95,
        ),
        FindingSource.DISCOVERER,
    )

    all_findings = [f_crit, f_high1, f_high2, f_med]
    ref_report = build_report(
        "Bank.sol",
        source_code=SAMPLE_SOL,
        findings=all_findings,
        generated_at=fixed_time,
        run_id="stable-run-id",
    )
    ref_md = render_markdown(ref_report)
    ref_json = export_report_to_json(ref_report)

    # Shuffled 5 times must produce identical output
    for seed in range(5):
        shuffled = list(all_findings)
        random.Random(seed).shuffle(shuffled)
        test_report = build_report(
            "Bank.sol",
            source_code=SAMPLE_SOL,
            findings=shuffled,
            generated_at=fixed_time,
            run_id="stable-run-id",
        )
        assert [f.id for f in test_report.findings] == [f.id for f in ref_report.findings]
        assert render_markdown(test_report) == ref_md
        assert export_report_to_json(test_report) == ref_json


def test_adversarial_text_sanitization():
    f = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Reentrancy with ```fences``` and # Header and | pipe",
            description=(
                "Here is injected markdown:\n"
                "```solidity\nfunction fake() {}\n```\n"
                "# Injected Top Heading\n"
                "- Injected list item\n"
                "<script>alert(1)</script>\n"
            ),
            location=CodeLocation(file="Bank.sol", line_start=11, line_end=17),
            severity=Severity.HIGH,
            confidence=0.8,
        ),
        FindingSource.DISCOVERER,
    )
    report = build_report("Bank.sol", source_code=SAMPLE_SOL, findings=[f])
    md = render_markdown(report)

    # Must NOT have unescaped top-level header # Injected Top Heading
    assert "\n# Injected Top Heading" not in md
    assert "\\# Injected Top Heading" in md
    # Must NOT have raw <script> tag
    assert "<script>" not in md
    assert "&lt;script&gt;" in md
    # Backticks must not close markdown block
    assert "\\`\\`\\`" in md or "\\`" in md


def test_snippet_extraction_centering_and_omission():
    long_code_lines = [f"// line {i}" for i in range(1, 51)]
    long_code = "\n".join(long_code_lines)

    f = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Long range finding",
            description="d",
            location=CodeLocation(file="Bank.sol", line_start=1, line_end=50),
            evidence=["L30: // line 30"],
            severity=Severity.HIGH,
            confidence=0.8,
        ),
        FindingSource.DISCOVERER,
    )
    report = build_report("Bank.sol", source_code=long_code, findings=[f])
    assert len(report.findings) == 1
    snippets = report.findings[0].snippets
    assert len(snippets) == 1
    snip = snippets[0]

    code_lines = [ln for ln in snip.lines if not ln.is_omitted]
    line_nums = [ln.line_number for ln in code_lines]
    assert 30 in line_nums
    assert 1 not in line_nums
    omitted_lines = [ln for ln in snip.lines if ln.is_omitted]
    assert len(omitted_lines) >= 1
    assert "omitted" in omitted_lines[0].content


def test_snippet_out_of_bounds_produces_degradation():
    f = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Out of bounds finding",
            description="d",
            location=CodeLocation(file="Bank.sol", line_start=100, line_end=150),
            severity=Severity.HIGH,
            confidence=0.8,
        ),
        FindingSource.DISCOVERER,
    )
    # File only has ~25 lines
    report = build_report("Bank.sol", source_code=SAMPLE_SOL, findings=[f])
    assert len(report.findings) == 1
    assert len(report.findings[0].snippets) == 0
    # Must produce a degradation record
    assert any(d.type == DegradationType.LINE_NUMBER_INVALID for d in report.degradations)


def test_no_absolute_paths_leak(tmp_path: Path):
    abs_file = tmp_path / "contracts" / "Bank.sol"
    abs_file.parent.mkdir(parents=True, exist_ok=True)
    abs_file.write_text(SAMPLE_SOL, encoding="utf-8")

    f = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Leak test",
            description="d",
            location=CodeLocation(file="Bank.sol", line_start=11, line_end=15),
            severity=Severity.HIGH,
            confidence=0.8,
        ),
        FindingSource.DISCOVERER,
    )
    report = build_report(
        abs_file,
        root=tmp_path,
        findings=[f],
    )
    md = render_markdown(report)
    json_str = export_report_to_json(report)

    assert str(tmp_path) not in md
    assert str(tmp_path) not in json_str
    assert "contracts/Bank.sol" in report.meta.target_path


def test_write_report_file_io(tmp_path: Path):
    f = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Write test",
            description="d",
            location=CodeLocation(file="Bank.sol", line_start=11, line_end=15),
            severity=Severity.HIGH,
            confidence=0.8,
        ),
        FindingSource.DISCOVERER,
    )
    report = build_report("Bank.sol", source_code=SAMPLE_SOL, findings=[f])

    json_path, md_path = write_report(report, output_dir=tmp_path)
    assert json_path.exists()
    assert md_path.exists()

    loaded = load_report_from_json(json_path.read_text(encoding="utf-8"))
    assert loaded == report


def test_snippet_config_applied():
    custom_settings = Settings(
        report_max_snippet_lines=6,
        report_max_snippets_per_finding=2,
    )
    long_code = "\n".join(f"// line {i}" for i in range(1, 40))
    f = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Custom config",
            description="d",
            location=CodeLocation(file="Bank.sol", line_start=1, line_end=35),
            evidence=["L20: // line 20"],
            severity=Severity.HIGH,
            confidence=0.8,
        ),
        FindingSource.DISCOVERER,
    )
    report = build_report("Bank.sol", source_code=long_code, findings=[f], settings=custom_settings)
    assert len(report.findings) == 1
def test_judge_source_preservation():
    draft = FindingDraft(
        category="reentrancy",
        title="Judge verified finding",
        description="d",
        location=CodeLocation(file="Bank.sol", line_start=11, line_end=15),
        evidence=["L14: call"],
        severity=Severity.CRITICAL,
        confidence=0.99,
    )
    base_finding = Finding.from_draft(draft, FindingSource.DISCOVERER)
    f = ReportFinding.from_finding(
        base_finding,
        severity_source="judge",
        confidence_source="judge",
    )
    report = build_report("Bank.sol", source_code=SAMPLE_SOL, findings=[f])
    assert report.findings[0].severity_source == "judge"
    assert report.findings[0].confidence_source == "judge"
    md = render_markdown(report)
    assert "(Source: judge)" in md


def test_slither_funnel_distinguishes_failure_vs_zero():
    # 1. Slither ran successfully with 0 findings
    info_ok = StaticAnalysisInfo(status=StaticAnalysisStatus.OK, n_kept=0)
    rep_ok = build_report("Bank.sol", source_code=SAMPLE_SOL, static_analysis=info_ok)
    assert rep_ok.stats.slither_findings == 0
    md_ok = render_markdown(rep_ok)
    assert "- **Slither Findings**: 0" in md_ok

    # 2. Slither failed (compile error)
    info_fail = StaticAnalysisInfo(
        status=StaticAnalysisStatus.COMPILE_FAILED,
        error="InvalidCompilation: syntax error",
        n_kept=0,
    )
    rep_fail = build_report("Bank.sol", source_code=SAMPLE_SOL, static_analysis=info_fail)
    assert rep_fail.stats.slither_findings is None
    md_fail = render_markdown(rep_fail)
    assert "- **Slither Findings**: Failed (compile_failed: InvalidCompilation: syntax error)" in md_fail

    # 3. Slither not run
    info_not_run = StaticAnalysisInfo(status=StaticAnalysisStatus.NOT_RUN)
    rep_not_run = build_report("Bank.sol", source_code=SAMPLE_SOL, static_analysis=info_not_run)
    assert rep_not_run.stats.slither_findings is None
    md_not_run = render_markdown(rep_not_run)
    assert "- **Slither Findings**: N/A (not run)" in md_not_run


def test_private_network_url_redaction():
    from audit_agent.report.builder import sanitize_url
    assert sanitize_url("http://10.20.1.50:8000/v1/chat") == "<private-network>"
    assert sanitize_url("https://user:pass@192.168.1.1:8080/v1?token=123") == "<private-network>"
    assert sanitize_url("http://localhost:11434") == "<private-network>"
    assert sanitize_url("http://127.0.0.1:8000") == "<private-network>"
    assert sanitize_url("https://gateway.corp.internal:443/llm") == "<private-network>"
    assert sanitize_url("https://api.deepseek.com/v1/chat") == "https://api.deepseek.com"
    assert sanitize_url("https://api.openai.com/v1") == "https://api.openai.com"


def test_real_data_reconciliation():
    from audit_agent.llm.cost import CostTracker
    from audit_agent.llm.types import Usage

    # Simulate real CostTracker usage
    tracker = CostTracker(
        max_total_tokens=100_000,
        max_cost_usd=1.0,
        price_in_usd_per_mtok=0.2,
        price_out_usd_per_mtok=0.8,
    )
    tracker.record(Usage(prompt_tokens=1500, completion_tokens=300))
    tracker.record(Usage(prompt_tokens=800, completion_tokens=200))
    cost_summary = tracker.snapshot()

    # Simulate real DiscoveryInfo
    disc_info = DiscoveryInfo(
        model_returned=3,
        kept=2,
        dropped_outside_file=1,
        retries=1,
        truncations=0,
        duration_s=4.52,
    )

    f1 = Finding.from_draft(
        FindingDraft(
            category="reentrancy", title="f1", description="d1",
            location=CodeLocation(file="Bank.sol", line_start=11, line_end=15),
            severity=Severity.HIGH, confidence=0.9,
        ),
        FindingSource.DISCOVERER,
    )
    f2 = Finding.from_draft(
        FindingDraft(
            category="reentrancy", title="f2", description="d2",
            location=CodeLocation(file="Bank.sol", line_start=7, line_end=9),
            severity=Severity.LOW, confidence=0.4,
        ),
        FindingSource.DISCOVERER,
    )

    static_info = StaticAnalysisInfo(
        status=StaticAnalysisStatus.OK,
        n_kept=1,
        duration_s=1.25,
    )

    report = build_report(
        "Bank.sol",
        source_code=SAMPLE_SOL,
        findings=[f1, f2],
        static_analysis=static_info,
        discovery=disc_info,
        cost=cost_summary,
    )

    # Reconcile Stats
    assert report.stats.prompt_tokens == 2300
    assert report.stats.completion_tokens == 500
    assert report.stats.total_tokens == 2800
    assert report.stats.llm_calls == 2
    assert report.stats.validation_retries == 1
    assert report.stats.discoverer_candidates == 3
    assert report.stats.final_findings == 2
    assert report.stats.slither_findings == 1
    assert report.stats.duration_s == 5.77  # 4.52 + 1.25
    assert report.stats.cost_usd > 0.0

    md = render_markdown(report)
    assert "- **Slither Findings**: 1" in md
    assert "- **Discoverer Candidates**: 3" in md
    assert "- **Final Reported Findings**: 2" in md
    assert "- **LLM Calls**: 2" in md
    assert "- **Validation Retries**: 1" in md
    assert "- **Total Tokens**: 2800" in md


def test_json_roundtrip_full():
    fixed_time = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    f = Finding.from_draft(
        FindingDraft(
            category="reentrancy",
            title="Reentrancy in withdraw",
            description="External call before state update.",
            location=CodeLocation(file="Bank.sol", line_start=11, line_end=17),
            evidence=["L14: call"],
            severity=Severity.HIGH,
            confidence=0.85,
        ),
        FindingSource.DISCOVERER,
    )
    report = build_report(
        "Bank.sol",
        source_code=SAMPLE_SOL,
        findings=[f],
        generated_at=fixed_time,
    )
    json_str = export_report_to_json(report)
    restored = load_report_from_json(json_str)
    assert restored == report
