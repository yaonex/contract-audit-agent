import pytest
from pydantic import ValidationError

from audit_agent.schemas.finding import FindingStatus
from audit_agent.schemas.poc import (
    IntegrityCheck, PocAttempt, PocOutcome, RejectionBasis, VerificationResult,
)


def att(outcome: PocOutcome, checked: bool = True, passed: bool = True, n: int = 1) -> PocAttempt:
    checks = [IntegrityCheck(name="imports_original", passed=passed)] if checked else []
    return PocAttempt(round_no=n, poc_source="// poc", outcome=outcome, integrity_checks=checks)


def result(status, attempts, basis=None) -> VerificationResult:
    return VerificationResult(
        finding_id="f1", status=status, attempts=attempts, rejection_basis=basis
    )


def test_empty_integrity_checks_do_not_count_as_passed():
    assert att(PocOutcome.PASSED, checked=False).integrity_ok is False


def test_confirmed_ok():
    r = result(FindingStatus.CONFIRMED, [att(PocOutcome.COMPILE_ERROR), att(PocOutcome.PASSED, n=2)])
    assert r.status == FindingStatus.CONFIRMED


def test_confirmed_requires_last_attempt_passed():
    with pytest.raises(ValidationError):
        result(FindingStatus.CONFIRMED, [att(PocOutcome.PASSED), att(PocOutcome.COMPILE_ERROR, n=2)])


def test_confirmed_requires_integrity_checks():
    with pytest.raises(ValidationError):
        result(FindingStatus.CONFIRMED, [att(PocOutcome.PASSED, checked=False)])
    with pytest.raises(ValidationError):
        result(FindingStatus.CONFIRMED, [att(PocOutcome.PASSED, passed=False)])


def test_rejected_ok():
    r = result(
        FindingStatus.REJECTED,
        [att(PocOutcome.ASSERTION_FAILED)],
        RejectionBasis.POC_ASSERTION_FAILED,
    )
    assert r.rejection_basis == RejectionBasis.POC_ASSERTION_FAILED


def test_rejected_requires_basis():
    with pytest.raises(ValidationError):
        result(FindingStatus.REJECTED, [att(PocOutcome.ASSERTION_FAILED)])


def test_static_evidence_forbidden_in_mvp():
    with pytest.raises(ValidationError):
        result(FindingStatus.REJECTED, [att(PocOutcome.ASSERTION_FAILED)], RejectionBasis.STATIC_EVIDENCE)


@pytest.mark.parametrize(
    "bad", [PocOutcome.COMPILE_ERROR, PocOutcome.SANDBOX_ERROR, PocOutcome.TIMEOUT]
)
def test_broken_verification_is_never_a_rejection(bad):
    with pytest.raises(ValidationError):
        result(FindingStatus.REJECTED, [att(bad)], RejectionBasis.POC_ASSERTION_FAILED)


def test_cheating_poc_failure_is_not_evidence():
    with pytest.raises(ValidationError):
        result(
            FindingStatus.REJECTED,
            [att(PocOutcome.ASSERTION_FAILED, passed=False)],
            RejectionBasis.POC_ASSERTION_FAILED,
        )


def test_rejected_contradicted_by_a_valid_pass():
    with pytest.raises(ValidationError):
        result(
            FindingStatus.REJECTED,
            [att(PocOutcome.ASSERTION_FAILED), att(PocOutcome.PASSED, n=2)],
            RejectionBasis.POC_ASSERTION_FAILED,
        )


def test_unverified_is_the_default_safe_landing():
    r = result(FindingStatus.UNVERIFIED, [att(PocOutcome.COMPILE_ERROR)])
    assert r.status == FindingStatus.UNVERIFIED
    with pytest.raises(ValidationError):
        result(FindingStatus.UNVERIFIED, [], RejectionBasis.POC_ASSERTION_FAILED)


def test_candidate_is_not_a_verdict():
    with pytest.raises(ValidationError):
        result(FindingStatus.CANDIDATE, [])