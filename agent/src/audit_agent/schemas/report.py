"""Report schemas (minimal version). Judge-related fields are added in Phase 1.4."""
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from audit_agent.schemas.finding import Finding


def _now() -> datetime:
    return datetime.now(UTC)


class RunMeta(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    model: str
    prompt_version: str = "reentrancy-v1"
    # External tool versions (slither, solc). Detectors change between versions, so results
    # are only comparable when these match.
    tool_versions: dict[str, str] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None


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
    target: str                                   # audited file/dir path
    cost: CostSummary = Field(default_factory=CostSummary)
    static_analysis: StaticAnalysisInfo = Field(default_factory=StaticAnalysisInfo)
    discovery: DiscoveryInfo = Field(default_factory=DiscoveryInfo)
    context: ContextInfo | None = None
    findings: list[Finding] = Field(default_factory=list)

    def counts_by_status(self) -> dict[str, int]:
        return dict(Counter(f.status.value for f in self.findings))