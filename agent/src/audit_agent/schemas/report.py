"""Report schemas (minimal version). Judge-related fields are added in Phase 1.4."""
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from audit_agent.schemas.finding import Finding, FindingStatus, Severity


def _now() -> datetime:
    return datetime.now(UTC)


class DegradationType(StrEnum):
    STATIC_ANALYSIS_FAILED = "static_analysis_failed"
    CONTEXT_TRUNCATED = "context_truncated"
    LLM_VALIDATION_RETRY = "llm_validation_retry"
    LINE_NUMBER_INVALID = "line_number_invalid"
    OTHER = "other"


class DegradationRecord(BaseModel):
    stage: str
    type: DegradationType
    description: str
    impact: str = ""
    location: str | None = None


class SnippetLine(BaseModel):
    line_number: int | None = None  # None for omission/ellipsis markers
    content: str
    is_omitted: bool = False


class CodeSnippet(BaseModel):
    file: str
    start_line: int
    end_line: int
    lines: list[SnippetLine] = Field(default_factory=list)

    def render(self) -> str:
        """Render snippet as formatted lines with line numbers and omission markers."""
        if not self.lines:
            return ""
        max_num = max((ln.line_number for ln in self.lines if ln.line_number is not None), default=0)
        width = max(len(str(max_num)), 2)
        out: list[str] = []
        for line in self.lines:
            if line.is_omitted or line.line_number is None:
                out.append(f"{'':>{width}} | {line.content}")
            else:
                out.append(f"{line.line_number:>{width}} | {line.content}")
        return "\n".join(out)


class ReportFinding(Finding):
    """Finding augmented with report presentation fields (code snippets, assessment sources)."""

    snippets: list[CodeSnippet] = Field(default_factory=list)
    severity_source: str = "discoverer"
    confidence_source: str = "discoverer"
    exploit_path: str | None = None
    recommendation: str | None = None

    @classmethod
    def from_finding(
        cls,
        finding: Finding,
        *,
        status: FindingStatus | None = None,
        snippets: list[CodeSnippet] | None = None,
        severity_source: str = "discoverer",
        confidence_source: str = "discoverer",
        exploit_path: str | None = None,
        recommendation: str | None = None,
    ) -> "ReportFinding":
        """Construct a ReportFinding from an existing Finding, applying report fields."""
        data = finding.model_dump()
        if status is not None:
            data["status"] = status
        data["snippets"] = snippets or []
        data["severity_source"] = severity_source
        data["confidence_source"] = confidence_source
        data["exploit_path"] = exploit_path or getattr(finding, "exploit_path", None)
        data["recommendation"] = recommendation or getattr(finding, "recommendation", None)
        return cls.model_validate(data)


class RunMeta(BaseModel):
    schema_version: str = "0.1.0"
    run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    model: str = ""
    prompt_version: str = "reentrancy.v1"
    # External tool versions (slither, solc). Detectors change between versions, so results
    # are only comparable when these match.
    tool_versions: dict[str, str] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    generated_at: datetime = Field(default_factory=_now)
    target_path: str = ""
    target_hash: str | None = None
    code_version: str | None = None


class RunConfig(BaseModel):
    model: str = ""
    provider: str = ""
    prompt_version: str = "reentrancy.v1"
    max_total_tokens: int = 200_000
    max_cost_usd: float = 0.5
    rag_enabled: bool = False
    verifier_enabled: bool = False
    judge_enabled: bool = False
    covered_categories: list[str] = Field(default_factory=lambda: ["reentrancy"])


class ReportStats(BaseModel):
    slither_findings: int | None = None
    discoverer_candidates: int = 0
    final_findings: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    llm_calls: int = 0
    validation_retries: int = 0


class CostSummary(BaseModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    llm_calls: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class StaticAnalysisStatus(StrEnum):
    NOT_RUN = "not_run"                # disabled, e.g. an LLM-only run
    OK = "ok"
    TOOL_MISSING = "tool_missing"      # slither cannot be started, or solc is not usable
    COMPILE_FAILED = "compile_failed"  # solc rejected the contract (syntax, pragma/solc mismatch)
    TIMEOUT = "timeout"
    FAILED = "failed"                  # anything else: crash, unreadable or unexpected output


class StaticAnalysisInfo(BaseModel):
    """What happened in the static-analysis step. Nothing is dropped silently: results that
    were filtered or unusable are counted here."""

    status: StaticAnalysisStatus = StaticAnalysisStatus.NOT_RUN
    error: str | None = None            # short, human-readable reason when status != ok
    n_results: int = Field(default=0, ge=0)     # detector results in the tool's output
    n_kept: int = Field(default=0, ge=0)        # converted to findings
    n_filtered: int = Field(default=0, ge=0)    # below min_impact (or optimization)
    n_unusable: int = Field(default=0, ge=0)    # no source location, could not be converted
    min_impact: str | None = None
    duration_s: float = Field(default=0.0, ge=0.0)
    raw_output_path: str | None = None  # raw json (or stderr on failure), relative to the trace dir


class DiscoveryInfo(BaseModel):
    """What happened in the LLM discovery step. Nothing is dropped silently: all filtering
    and grounding discards are counted here."""

    model_returned: int = Field(default=0, ge=0)
    kept: int = Field(default=0, ge=0)
    dropped_outside_file: int = Field(default=0, ge=0)
    dropped_in_omitted: int = Field(default=0, ge=0)
    dropped_invalid_range: int = Field(default=0, ge=0)
    dropped_no_verified_quote: int = Field(default=0, ge=0)
    dropped_duplicate: int = Field(default=0, ge=0)
    dropped_over_limit: int = Field(default=0, ge=0)
    names_cleared: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    truncations: int = Field(default=0, ge=0)
    duration_s: float = Field(default=0.0, ge=0.0)


class ContextInfo(BaseModel):
    """What the model was shown of the contract. A partial view must never pass for a full
    audit, so the coverage is part of the report."""

    file: str                                   # relative to the audit root, never absolute
    total_lines: int = Field(ge=1)
    included_ranges: list[tuple[int, int]] = Field(default_factory=list)   # 1-based, inclusive
    truncated: bool = False                     # whole lines were left out
    est_tokens: int = Field(default=0, ge=0)    # estimated, see llm/cost.py
    max_tokens: int = Field(default=0, ge=0)
    n_long_lines: int = Field(default=0, ge=0)  # lines in the file longer than the per-line cap
    imports: list[str] = Field(default_factory=list)   # import statements: NOT part of the context
    had_decode_errors: bool = False             # bytes that were not valid UTF-8 were replaced


class AuditReport(BaseModel):
    meta: RunMeta
    run_config: RunConfig = Field(default_factory=RunConfig)
    target: str = ""                               # audited file/dir path
    findings: list[ReportFinding] = Field(default_factory=list)
    stats: ReportStats = Field(default_factory=ReportStats)
    degradations: list[DegradationRecord] = Field(default_factory=list)
    cost: CostSummary = Field(default_factory=CostSummary)
    static_analysis: StaticAnalysisInfo = Field(default_factory=StaticAnalysisInfo)
    discovery: DiscoveryInfo = Field(default_factory=DiscoveryInfo)
    context: ContextInfo | None = None

    @field_validator("findings", mode="before")
    @classmethod
    def _coerce_findings(cls, v: Any) -> Any:
        if isinstance(v, list):
            res = []
            for item in v:
                if isinstance(item, Finding) and not isinstance(item, ReportFinding):
                    res.append(ReportFinding(**item.model_dump()))
                else:
                    res.append(item)
            return res
        return v

    def counts_by_status(self) -> dict[str, int]:
        return dict(Counter(f.status.value for f in self.findings))

    def counts_by_severity(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for f in self.findings:
            counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
        return counts