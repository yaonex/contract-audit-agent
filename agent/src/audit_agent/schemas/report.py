"""Report schemas (minimal version). Judge-related fields are added in Phase 1.4."""
from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from audit_agent.schemas.finding import Finding


def _now() -> datetime:
    return datetime.now(UTC)


class RunMeta(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    model: str
    prompt_version: str
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


class AuditReport(BaseModel):
    meta: RunMeta
    target: str                                   # audited file/dir path
    cost: CostSummary = Field(default_factory=CostSummary)
    findings: list[Finding] = Field(default_factory=list)

    def counts_by_status(self) -> dict[str, int]:
        return dict(Counter(f.status.value for f in self.findings))