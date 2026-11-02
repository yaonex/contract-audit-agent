from pathlib import Path

import pytest
from pydantic import ValidationError

from audit_agent.agents.grounding import ground_findings
from audit_agent.context.source import build_context, build_context_from_text
from audit_agent.schemas.finding import (
    DiscovererItem,
    DiscovererOutput,
    FindingSource,
    Severity,
)

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "samples"


@pytest.fixture
def bank_vulnerable_ctx():
    return build_context(SAMPLES_DIR / "vulnerable" / "Bank.sol")


@pytest.fixture
def bank_safe_ctx():
    return build_context(SAMPLES_DIR / "safe" / "Bank.sol")


@pytest.fixture
def vault_vulnerable_ctx():
    return build_context(SAMPLES_DIR / "vulnerable" / "Vault.sol")


def test_valid_finding_kept_and_fields_correct(bank_vulnerable_ctx):
    item = DiscovererItem(
        title="Reentrancy in withdraw",
        description="External call before balances zeroing",
        line_start=11,
        line_end=17,
        function="withdraw",
        contract="Bank",
        quotes=['14 | (bool ok, ) = msg.sender.call{value: amount}("");', "balances[msg.sender] = 0;"],
        severity=Severity.CRITICAL,
        confidence=0.95,
    )
    output = DiscovererOutput(findings=[item])
    findings, info = ground_findings(output, bank_vulnerable_ctx, "reentrancy")

    assert len(findings) == 1
    f = findings[0]
    assert f.category == "reentrancy"
    assert f.source == FindingSource.DISCOVERER
    assert f.rule_id is None
    assert f.location.file == bank_vulnerable_ctx.file
    assert f.location.line_start == 11
    assert f.location.line_end == 17
    assert f.location.function == "withdraw"
    assert f.location.contract == "Bank"
    assert f.severity == Severity.CRITICAL
    assert f.confidence == 0.95
    assert len(f.evidence) == 2
    assert f.evidence[0] == 'L14: (bool ok, ) = msg.sender.call{value: amount}("");'
    assert f.evidence[1] == "L16: balances[msg.sender] = 0;"
    assert info.model_returned == 1
    assert info.kept == 1
    assert info.dropped_no_verified_quote == 0


def test_quote_prefix_stripping_and_multiline(bank_vulnerable_ctx):
    # Multi-line quote with whitespace differences and line prefixes
    raw_quote = """
    14 | (bool ok, ) = msg.sender.call{value: amount}("");
    15 | require(ok, "transfer failed");
    """
    item = DiscovererItem(
        title="Reentrancy",
        description="Call then check",
        line_start=11,
        line_end=17,
        quotes=[raw_quote],
        severity=Severity.HIGH,
        confidence=0.9,
    )
    findings, _ = ground_findings(DiscovererOutput(findings=[item]), bank_vulnerable_ctx, "reentrancy")
    assert len(findings) == 1
    assert len(findings[0].evidence) == 1
    assert findings[0].evidence[0].startswith("L14: ")
    assert '(bool ok, ) = msg.sender.call{value: amount}(""); require(ok, "transfer failed");' in findings[0].evidence[0]


def test_quote_not_in_range_dropped(bank_vulnerable_ctx):
    # Quote from line 14, but cited range is 7-9 (deposit function)
    item = DiscovererItem(
        title="Mislocated quote",
        description="Wrong line range",
        line_start=7,
        line_end=9,
        quotes=['(bool ok, ) = msg.sender.call{value: amount}("");'],
        severity=Severity.HIGH,
        confidence=0.8,
    )
    findings, info = ground_findings(DiscovererOutput(findings=[item]), bank_vulnerable_ctx, "reentrancy")
    assert findings == []
    assert info.model_returned == 1
    assert info.kept == 0
    assert info.dropped_no_verified_quote == 1


def test_partial_quotes_verified(bank_vulnerable_ctx):
    item = DiscovererItem(
        title="Partial valid quotes",
        description="One quote valid, one fabricated",
        line_start=11,
        line_end=17,
        quotes=[
            "balances[msg.sender] = 0;",
            "totally_invented_call();",
        ],
        severity=Severity.HIGH,
        confidence=0.8,
    )
    findings, _ = ground_findings(DiscovererOutput(findings=[item]), bank_vulnerable_ctx, "reentrancy")
    assert len(findings) == 1
    assert len(findings[0].evidence) == 1
    assert findings[0].evidence[0] == "L16: balances[msg.sender] = 0;"


def test_range_checks_dropped_and_counted(bank_vulnerable_ctx):
    bad_range = DiscovererItem(
        title="Inverted range",
        description="test",
        line_start=15,
        line_end=11,
        quotes=["balances[msg.sender] = 0;"],
        severity=Severity.LOW,
        confidence=0.5,
    )
    outside = DiscovererItem(
        title="Outside file",
        description="test",
        line_start=100,
        line_end=110,
        quotes=["nonexistent"],
        severity=Severity.LOW,
        confidence=0.5,
    )
    output = DiscovererOutput(findings=[bad_range, outside])
    findings, info = ground_findings(output, bank_vulnerable_ctx, "reentrancy")
    assert findings == []
    assert info.dropped_invalid_range == 1
    assert info.dropped_outside_file == 1
    assert info.model_returned == 2
    assert info.kept == 0


def test_in_omitted_range_dropped():
    long_code = "\n".join([f"uint256 public var_{i};" for i in range(1000)])
    ctx = build_context_from_text(long_code, file="Long.sol", max_tokens=8_000)
    assert ctx.truncated
    gap_start, gap_end = ctx.omitted_ranges[0]

    item = DiscovererItem(
        title="Omitted line finding",
        description="Hits omitted lines",
        line_start=gap_start,
        line_end=gap_end,
        quotes=["uint256 public var_500;"],
        severity=Severity.LOW,
        confidence=0.5,
    )
    findings, info = ground_findings(DiscovererOutput(findings=[item]), ctx, "reentrancy")
    assert findings == []
    assert info.dropped_in_omitted == 1
    assert info.kept == 0


def test_names_cleared_when_not_in_source(bank_vulnerable_ctx):
    item = DiscovererItem(
        title="Made up names",
        description="test",
        line_start=11,
        line_end=17,
        function="withdrawFunds",
        contract="BankVault",
        quotes=["balances[msg.sender] = 0;"],
        severity=Severity.HIGH,
        confidence=0.9,
    )
    findings, info = ground_findings(DiscovererOutput(findings=[item]), bank_vulnerable_ctx, "reentrancy")
    assert len(findings) == 1
    assert findings[0].location.function is None
    assert findings[0].location.contract is None
    assert info.names_cleared == 2


def test_identifier_substring_not_matched(vault_vulnerable_ctx):
    item = DiscovererItem(
        title="Substring check",
        description="test",
        line_start=13,
        line_end=15,
        function="own",
        quotes=["owner = newOwner;"],
        severity=Severity.HIGH,
        confidence=0.9,
    )
    findings, info = ground_findings(DiscovererOutput(findings=[item]), vault_vulnerable_ctx, "access_control")
    assert len(findings) == 1
    assert findings[0].location.function is None
    assert info.names_cleared == 1


def test_dedup_keeps_higher_confidence(bank_vulnerable_ctx):
    item1 = DiscovererItem(
        title="Low confidence duplicate",
        description="First",
        line_start=11,
        line_end=17,
        function="withdraw",
        quotes=["balances[msg.sender] = 0;"],
        severity=Severity.HIGH,
        confidence=0.4,
    )
    item2 = DiscovererItem(
        title="High confidence duplicate",
        description="Second",
        line_start=11,
        line_end=17,
        function="withdraw",
        quotes=["balances[msg.sender] = 0;"],
        severity=Severity.CRITICAL,
        confidence=0.9,
    )
    output = DiscovererOutput(findings=[item1, item2])
    findings, info = ground_findings(output, bank_vulnerable_ctx, "reentrancy")
    assert len(findings) == 1
    assert findings[0].confidence == 0.9
    assert findings[0].title == "High confidence duplicate"
    assert info.dropped_duplicate == 1
    assert info.kept == 1


def test_over_limit_capped_at_10(vault_vulnerable_ctx):
    items = [
        DiscovererItem(
            title=f"Finding {i}",
            description="desc",
            line_start=i + 1,
            line_end=20,
            quotes=["owner = newOwner;"] if i + 1 <= 14 else ["to.transfer(address(this).balance);"],
            severity=Severity.LOW,
            confidence=round(0.05 * (i + 1), 2),
        )
        for i in range(12)
    ]
    output = DiscovererOutput(findings=items)
    findings, info = ground_findings(output, vault_vulnerable_ctx, "other")
    assert len(findings) == 10
    assert info.dropped_over_limit == 2
    assert info.kept == 10
    confidences = [f.confidence for f in findings]
    assert confidences == sorted(confidences, reverse=True)


def test_schema_normalization_and_validation():
    item = DiscovererItem.model_validate({
        "title": "T",
        "description": "D",
        "line_start": "11",
        "line_end": "17",
        "quotes": ["code"],
        "severity": "  HIGH ",
        "confidence": "1.5",
    })
    assert item.severity == Severity.HIGH
    assert item.confidence == 1.0
    assert item.line_start == 11
    assert item.line_end == 17

    item2 = DiscovererItem.model_validate({
        "title": "T",
        "description": "D",
        "line_start": 1,
        "line_end": 2,
        "quotes": ["code"],
        "severity": "low",
        "confidence": -0.5,
    })
    assert item2.confidence == 0.0

    with pytest.raises(ValidationError):
        DiscovererOutput.model_validate({})

    empty = DiscovererOutput.model_validate({"findings": []})
    assert empty.findings == []


def test_grounding_invariant_comprehensive(bank_vulnerable_ctx):
    items = [
        DiscovererItem(
            title="Kept 1",
            description="d",
            line_start=11,
            line_end=17,
            function="withdraw",
            quotes=["balances[msg.sender] = 0;"],
            severity=Severity.HIGH,
            confidence=0.8,
        ),
        DiscovererItem(
            title="Dup",
            description="d",
            line_start=11,
            line_end=17,
            function="withdraw",
            quotes=["balances[msg.sender] = 0;"],
            severity=Severity.HIGH,
            confidence=0.7,
        ),
        DiscovererItem(
            title="Inv",
            description="d",
            line_start=15,
            line_end=12,
            quotes=["balances[msg.sender] = 0;"],
            severity=Severity.HIGH,
            confidence=0.5,
        ),
        DiscovererItem(
            title="Out",
            description="d",
            line_start=50,
            line_end=60,
            quotes=["foo"],
            severity=Severity.HIGH,
            confidence=0.5,
        ),
        DiscovererItem(
            title="NoQuote",
            description="d",
            line_start=7,
            line_end=9,
            quotes=["invented code"],
            severity=Severity.HIGH,
            confidence=0.5,
        ),
    ]
    output = DiscovererOutput(findings=items)
    findings, info = ground_findings(output, bank_vulnerable_ctx, "reentrancy", retries=1, truncations=0)

    assert len(findings) == 1
    assert info.model_returned == 5
    assert info.kept == 1
    assert info.dropped_duplicate == 1
    assert info.dropped_invalid_range == 1
    assert info.dropped_outside_file == 1
    assert info.dropped_no_verified_quote == 1
    assert info.dropped_in_omitted == 0
    assert info.dropped_over_limit == 0
    assert info.retries == 1

    total_dropped = (
        info.dropped_outside_file
        + info.dropped_in_omitted
        + info.dropped_invalid_range
        + info.dropped_no_verified_quote
        + info.dropped_duplicate
        + info.dropped_over_limit
    )
    assert info.model_returned == info.kept + total_dropped

