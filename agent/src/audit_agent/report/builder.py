"""Report builder: transforms upstream findings, metrics, and context into an AuditReport."""
import hashlib
import ipaddress
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from audit_agent.config import Settings, get_settings
from audit_agent.schemas.finding import Finding, FindingSource, FindingStatus, Severity
from audit_agent.schemas.report import (
    AuditReport,
    CodeSnippet,
    ContextInfo,
    CostSummary,
    DegradationRecord,
    DegradationType,
    DiscoveryInfo,
    ReportFinding,
    ReportStats,
    RunConfig,
    RunMeta,
    SnippetLine,
    StaticAnalysisInfo,
    StaticAnalysisStatus,
)

logger = logging.getLogger(__name__)

_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}

_CORE_LINE_RE = re.compile(r"\bL(\d+)\b")


def sanitize_url(url: str | None) -> str:
    """Sanitize URL by stripping credentials, path, port, query, and redacting private hosts."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if not host:
            return "redacted"
        # Check for localhost / loopback / private IP / internal domain suffixes
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(
            (".local", ".internal", ".lan", ".corp", ".home", ".test")
        ):
            return "<private-network>"
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return "<private-network>"
        except ValueError:
            pass  # Not an IP address, regular domain name
        scheme = parts.scheme or "https"
        return f"{scheme}://{host}"
    except (ValueError, AttributeError):
        return "redacted"


def relative_display_path(path: Path | str, root: Path | str | None = None) -> str:
    """Format path as relative to root or cwd, never leaking absolute machine paths."""
    p = Path(path)
    base = Path(root).resolve() if root else Path.cwd().resolve()
    try:
        return p.resolve().relative_to(base).as_posix()
    except ValueError:
        return p.name


def extract_snippets(
    finding: Finding,
    source_lines: list[str],
    *,
    max_snippet_lines: int = 12,
    max_snippets: int = 3,
) -> tuple[list[CodeSnippet], list[DegradationRecord]]:
    """Extract code snippets from source_lines for a finding.

    - Centered on core lines identified from finding.evidence (or range midpoint).
    - Uses omission markers for lines outside the centered window.
    - If line range is invalid or out of bounds, returns empty snippets and a degradation record.
    """
    total_lines = len(source_lines)
    start = finding.location.line_start
    end = finding.location.line_end
    rel_file = finding.location.file

    degradations: list[DegradationRecord] = []

    # Validate line numbers
    if total_lines == 0 or start < 1 or end > total_lines or start > end:
        degradations.append(
            DegradationRecord(
                stage="snippet_extraction",
                type=DegradationType.LINE_NUMBER_INVALID,
                description=(
                    f"Line range {start}-{end} is invalid or outside source file {rel_file} "
                    f"(file has {total_lines} lines)"
                ),
                impact="Code snippet could not be extracted for this finding.",
                location=f"{finding.id} ({rel_file}:{start}-{end})",
            )
        )
        return [], degradations

    # Find core line numbers from evidence
    core_lines: list[int] = []
    for ev in finding.evidence:
        for match in _CORE_LINE_RE.finditer(ev):
            line_no = int(match.group(1))
            if start <= line_no <= end and line_no not in core_lines:
                core_lines.append(line_no)

    if not core_lines:
        core_lines = [(start + end) // 2]

    # Limit core lines to max_snippets
    core_lines = core_lines[:max_snippets]

    # If the whole range fits in max_snippet_lines, extract full range as one snippet
    span_len = end - start + 1
    if span_len <= max_snippet_lines:
        lines = [
            SnippetLine(line_number=i, content=source_lines[i - 1], is_omitted=False)
            for i in range(start, end + 1)
        ]
        return [CodeSnippet(file=rel_file, start_line=start, end_line=end, lines=lines)], []

    # Multi-line snippet: center around each core line
    snippets: list[CodeSnippet] = []
    seen_windows: list[tuple[int, int]] = []

    for core in core_lines:
        radius = (max_snippet_lines - 1) // 2
        w_start = max(start, core - radius)
        w_end = min(end, w_start + max_snippet_lines - 1)
        if w_end == end:
            w_start = max(start, w_end - max_snippet_lines + 1)

        # Avoid redundant snippets with exact same window
        if any(abs(w_start - s) < 2 and abs(w_end - e) < 2 for s, e in seen_windows):
            continue
        seen_windows.append((w_start, w_end))

        snippet_lines: list[SnippetLine] = []
        if w_start > start:
            omitted = w_start - start
            snippet_lines.append(
                SnippetLine(
                    line_number=None,
                    content=f"... [{omitted} lines omitted] ...",
                    is_omitted=True,
                )
            )

        for i in range(w_start, w_end + 1):
            snippet_lines.append(
                SnippetLine(line_number=i, content=source_lines[i - 1], is_omitted=False)
            )

        if w_end < end:
            omitted = end - w_end
            snippet_lines.append(
                SnippetLine(
                    line_number=None,
                    content=f"... [{omitted} lines omitted] ...",
                    is_omitted=True,
                )
            )

        snippets.append(
            CodeSnippet(file=rel_file, start_line=start, end_line=end, lines=snippet_lines)
        )

    return snippets[:max_snippets], []
def build_report(
    target_path: str | Path,
    *,
    source_code: str | None = None,
    root: str | Path | None = None,
    findings: list[Finding] | None = None,
    static_analysis: StaticAnalysisInfo | None = None,
    discovery: DiscoveryInfo | None = None,
    context: ContextInfo | None = None,
    cost: CostSummary | None = None,
    degradations: list[DegradationRecord] | None = None,
    run_config: RunConfig | None = None,
    tool_versions: dict[str, str] | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    generated_at: datetime | None = None,
    started_at: datetime | None = None,
    run_id: str | None = None,
    settings: Settings | None = None,
) -> AuditReport:
    """Build a complete AuditReport object.

    Handles snippet extraction, degradation mapping from upstream signals,
    deterministic finding ordering, and funnel stats computation.
    """
    cfg = settings or get_settings()
    rel_target = relative_display_path(target_path, root)

    target_file = Path(target_path)
    if source_code is None and target_file.is_file():
        try:
            source_code = target_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("Could not read target source file: %s", target_path)
            source_code = ""

    source_lines = (
        source_code.replace("\r\n", "\n").split("\n") if source_code is not None else []
    )
    target_hash = (
        hashlib.sha256(source_code.encode("utf-8")).hexdigest() if source_code else None
    )

    all_degradations: list[DegradationRecord] = []
    if degradations:
        all_degradations.extend(degradations)

    # Map upstream degradation signals
    if static_analysis and static_analysis.status not in (
        StaticAnalysisStatus.OK,
        StaticAnalysisStatus.NOT_RUN,
    ):
        all_degradations.append(
            DegradationRecord(
                stage="static_analysis",
                type=DegradationType.STATIC_ANALYSIS_FAILED,
                description=(
                    f"Static analysis failed with status '{static_analysis.status.value}': "
                    f"{static_analysis.error or 'unknown error'}"
                ),
                impact="Static analysis findings may be missing.",
            )
        )

    if context:
        if context.truncated:
            all_degradations.append(
                DegradationRecord(
                    stage="context_building",
                    type=DegradationType.CONTEXT_TRUNCATED,
                    description=(
                        f"Source context was truncated to {context.max_tokens} tokens "
                        f"({len(context.included_ranges)} ranges included)."
                    ),
                    impact="Portions of the contract source were omitted from the audit context.",
                    location=context.file,
                )
            )
        if context.had_decode_errors:
            all_degradations.append(
                DegradationRecord(
                    stage="context_building",
                    type=DegradationType.OTHER,
                    description="Source file contained invalid UTF-8 bytes that were replaced.",
                    impact="Some characters in the contract text may be distorted.",
                    location=context.file,
                )
            )

    if discovery:
        if discovery.retries > 0:
            all_degradations.append(
                DegradationRecord(
                    stage="discovery",
                    type=DegradationType.LLM_VALIDATION_RETRY,
                    description=(
                        f"Discoverer encountered {discovery.retries} structured output "
                        "validation retry attempts."
                    ),
                    impact="Potential model response formatting instability.",
                )
            )
        if discovery.truncations > 0:
            all_degradations.append(
                DegradationRecord(
                    stage="discovery",
                    type=DegradationType.CONTEXT_TRUNCATED,
                    description=(
                        f"Discoverer output was truncated {discovery.truncations} times "
                        "due to token budget limit."
                    ),
                    impact="Token budget had to be expanded during generation.",
                )
            )

    # Process findings: snippet extraction and MVP status assignment
    input_findings = findings or []
    report_findings: list[ReportFinding] = []

    for f in input_findings:
        snippets, snippet_degs = extract_snippets(
            f,
            source_lines,
            max_snippet_lines=cfg.report_max_snippet_lines,
            max_snippets=cfg.report_max_snippets_per_finding,
        )
        all_degradations.extend(snippet_degs)

        status = (
            FindingStatus.UNVERIFIED
            if f.status == FindingStatus.CANDIDATE
            else f.status
        )

        sev_source = getattr(f, "severity_source", None) or (
            "slither" if f.source == FindingSource.SLITHER else "discoverer"
        )
        conf_source = getattr(f, "confidence_source", None) or (
            "slither" if f.source == FindingSource.SLITHER else "discoverer"
        )

        rf = ReportFinding.from_finding(
            f,
            status=status,
            snippets=snippets,
            severity_source=sev_source,
            confidence_source=conf_source,
            exploit_path=getattr(f, "exploit_path", None),
            recommendation=getattr(f, "recommendation", None),
        )
        report_findings.append(rf)

    # Sort findings deterministically (Decision 10)
    report_findings.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 99),
            -round(float(f.confidence), 4),
            f.location.file,
            f.location.line_start,
            f.location.line_end,
            f.id,
        )
    )

    # Assemble Stats
    if static_analysis is None or static_analysis.status == StaticAnalysisStatus.NOT_RUN:
        slither_cnt = None
    elif static_analysis.status == StaticAnalysisStatus.OK:
        slither_cnt = static_analysis.n_kept
    else:
        # Failure: static analysis did not succeed, distinct from 0 findings
        slither_cnt = None
    candidate_cnt = (
        discovery.model_returned
        if discovery and discovery.model_returned > 0
        else len(input_findings)
    )
    p_tok = cost.prompt_tokens if cost else 0
    c_tok = cost.completion_tokens if cost else 0
    stats = ReportStats(
        slither_findings=slither_cnt,
        discoverer_candidates=candidate_cnt,
        final_findings=len(report_findings),
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
        total_tokens=p_tok + c_tok,
        cost_usd=round(cost.cost_usd, 6) if cost else 0.0,
        duration_s=round(
            (static_analysis.duration_s if static_analysis else 0.0)
            + (discovery.duration_s if discovery else 0.0),
            3,
        ),
        llm_calls=cost.llm_calls if cost else 0,
        validation_retries=discovery.retries if discovery else 0,
    )

    effective_model = model or (run_config.model if run_config else cfg.llm_model)
    effective_prompt = (
        prompt_version
        or (run_config.prompt_version if run_config else "reentrancy.v1")
    )
    meta = RunMeta(
        schema_version="0.1.0",
        run_id=run_id or uuid4().hex[:12],
        model=effective_model,
        prompt_version=effective_prompt,
        tool_versions=tool_versions or {},
        started_at=started_at or generated_at or datetime.now(UTC),
        generated_at=generated_at or datetime.now(UTC),
        target_path=rel_target,
        target_hash=target_hash,
    )

    config_obj = run_config or RunConfig(
        model=effective_model,
        provider=sanitize_url(cfg.llm_base_url),
        prompt_version=effective_prompt,
        max_total_tokens=cfg.max_total_tokens,
        max_cost_usd=cfg.max_cost_usd,
        rag_enabled=False,
        verifier_enabled=False,
        judge_enabled=False,
        covered_categories=["reentrancy"],
    )

    return AuditReport(
        meta=meta,
        run_config=config_obj,
        target=rel_target,
        findings=report_findings,
        stats=stats,
        degradations=all_degradations,
        cost=cost or CostSummary(),
        static_analysis=static_analysis or StaticAnalysisInfo(),
        discovery=discovery or DiscoveryInfo(),
        context=context,
    )

