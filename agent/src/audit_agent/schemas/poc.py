"""PoC (proof-of-concept) schemas.

A Verifier tries to prove a Finding by executing an exploit test. Every try is a
PocAttempt; the final verdict is a VerificationResult. The validators encode the
rule "PoC failed != not a vulnerability" so it cannot be violated by accident.
"""
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from audit_agent.schemas.finding import FindingStatus


class PocOutcome(StrEnum):
    COMPILE_ERROR = "compile_error"          # PoC itself is broken -> retry-able
    RUNTIME_ERROR = "runtime_error"          # compiled, but reverted unexpectedly
    ASSERTION_FAILED = "assertion_failed"    # ran fine, exploit did NOT succeed
    PASSED = "passed"                        # exploit succeeded
    TIMEOUT = "timeout"
    SANDBOX_ERROR = "sandbox_error"          # infra problem, not the PoC's fault


class RejectionBasis(StrEnum):
    POC_ASSERTION_FAILED = "poc_assertion_failed"
    STATIC_EVIDENCE = "static_evidence"      # reserved; forbidden in MVP


class IntegrityCheck(BaseModel):
    """Result of one anti-cheating check (e.g. 'imports original contract')."""

    name: str
    passed: bool
    detail: str = ""


class PocAttempt(BaseModel):
    round_no: int = Field(ge=1)
    poc_source: str                           # generated Solidity test code
    outcome: PocOutcome
    error_summary: str = ""                   # extracted, size-bounded (see forge_runner, Phase 1.4)
    log_path: str | None = None               # path relative to the trace dir (see Tracer.write_blob)
    duration_s: float = Field(default=0.0, ge=0.0)
    integrity_checks: list[IntegrityCheck] = Field(default_factory=list)

    @property
    def integrity_ok(self) -> bool:
        # Empty list means "never checked", which must NOT count as "passed".
        return bool(self.integrity_checks) and all(c.passed for c in self.integrity_checks)


class VerificationResult(BaseModel):
    finding_id: str
    status: FindingStatus
    attempts: list[PocAttempt] = Field(default_factory=list)
    rejection_basis: RejectionBasis | None = None
    rationale: str = ""

    @model_validator(mode="after")
    def _enforce_verdict_rules(self) -> "VerificationResult":
        if self.status == FindingStatus.CANDIDATE:
            raise ValueError("a verification result must be a verdict, not 'candidate'")

        if self.status == FindingStatus.CONFIRMED:
            if not self.attempts:
                raise ValueError("confirmed requires at least one attempt")
            last = self.attempts[-1]
            if last.outcome != PocOutcome.PASSED:
                raise ValueError("confirmed requires the last attempt to be 'passed'")
            if not last.integrity_ok:
                raise ValueError("confirmed requires the passing attempt to pass integrity checks")
            if self.rejection_basis is not None:
                raise ValueError("confirmed must not carry a rejection_basis")

        elif self.status == FindingStatus.REJECTED:
            if self.rejection_basis != RejectionBasis.POC_ASSERTION_FAILED:
                raise ValueError(
                    "MVP: rejected requires rejection_basis='poc_assertion_failed'"
                )
            good_failures = [
                a for a in self.attempts
                if a.outcome == PocOutcome.ASSERTION_FAILED and a.integrity_ok
            ]
            if not good_failures:
                raise ValueError(
                    "rejected requires an integrity-checked attempt with outcome 'assertion_failed'"
                )
            if any(a.outcome == PocOutcome.PASSED and a.integrity_ok for a in self.attempts):
                raise ValueError("rejected contradicts a passing, integrity-checked attempt")

        else:  # UNVERIFIED
            if self.rejection_basis is not None:
                raise ValueError("unverified must not carry a rejection_basis")

        return self