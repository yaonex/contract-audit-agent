"""Finding schemas.

FindingDraft = what the LLM must produce (small, validated contract).
Finding      = FindingDraft + system-assigned fields (id, status, source).
"""
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from audit_agent.schemas.taxonomy import load_taxonomy


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingStatus(StrEnum):
    CANDIDATE = "candidate"    # produced by Discoverer, not yet verified
    CONFIRMED = "confirmed"    # PoC executed successfully
    UNVERIFIED = "unverified"  # PoC failed/not attempted; NOT the same as "not a bug"
    REJECTED = "rejected"      # Verifier/Judge found positive evidence it is a false positive


class FindingSource(StrEnum):
    SLITHER = "slither"
    DISCOVERER = "discoverer"


class CodeLocation(BaseModel):
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    contract: str | None = None
    function: str | None = None

    @model_validator(mode="after")
    def _range_ok(self) -> "CodeLocation":
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class FindingDraft(BaseModel):
    """LLM-facing contract. Keep it minimal: only fields the model can judge."""

    category: str
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    location: CodeLocation
    evidence: list[str] = Field(default_factory=list)  # code snippets / reasoning steps
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_and_check_category(cls, v: object) -> str:
        # LLMs often return "Access Control" / "access-control"; normalize first.
        if not isinstance(v, str):
            raise ValueError("category must be a string")
        norm = v.strip().lower().replace("-", "_").replace(" ", "_")
        allowed = load_taxonomy().ids
        if norm not in allowed:
            raise ValueError(f"unknown category '{v}', must be one of {sorted(allowed)}")
        return norm


class Finding(FindingDraft):
    """System-side finding. id/status/source are never set by the LLM."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    status: FindingStatus = FindingStatus.CANDIDATE
    source: FindingSource = FindingSource.DISCOVERER

    @classmethod
    def from_draft(cls, draft: FindingDraft, source: FindingSource) -> "Finding":
        return cls(**draft.model_dump(), source=source)

    def fingerprint(self) -> str:
        """Coarse identity for dedup (Judge) — draft; matcher will use richer logic."""
        loc = self.location
        anchor = loc.function or f"L{loc.line_start}"
        return f"{self.category}|{loc.file}|{anchor}"