import json
from pathlib import Path

import pytest

from audit_agent.schemas.finding import FindingSource, Severity
from audit_agent.static_analysis.findings_parser import SlitherParseError, parse_slither_json

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "slither-0.11.6"
VULN_ROOT = Path("/ROOT/samples/vulnerable")


def load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


# ---- real outputs ---------------------------------------------------------------------

def test_reentrancy_finding_matches_the_ground_truth():
    parsed = parse_slither_json(load("bank_vulnerable"), root=VULN_ROOT)
    assert (parsed.n_results, parsed.n_filtered, parsed.n_unusable) == (2, 1, 0)
    (f,) = parsed.findings
    assert (f.rule_id, f.category, f.source) == ("reentrancy-eth", "reentrancy", FindingSource.SLITHER)
    assert (f.severity, f.confidence) == (Severity.HIGH, 0.6)
    loc = f.location
    assert (loc.file, loc.line_start, loc.line_end, loc.contract, loc.function) == (
        "Bank.sol", 11, 17, "Bank", "withdraw",
    )
    assert f.evidence == [
        "L14: (ok,None) = msg.sender.call{value: amount}()",
        "L16: balances[msg.sender] = 0",
    ]
    assert f.title == "reentrancy-eth in Bank.withdraw"


def test_informational_results_come_back_when_the_threshold_is_lowered():
    parsed = parse_slither_json(load("bank_vulnerable"), root=VULN_ROOT, min_impact="informational")
    # Same lines: the tie is broken by rule name, so the order is deterministic.
    assert [f.rule_id for f in parsed.findings] == ["low-level-calls", "reentrancy-eth"]
    low_level = parsed.findings[0]
    assert (low_level.category, low_level.severity, low_level.confidence) == ("other", Severity.INFO, 0.9)


def test_safe_bank_has_nothing_above_the_threshold():
    parsed = parse_slither_json(load("bank_safe"), root=Path("/ROOT/samples/safe"))
    assert parsed.findings == [] and parsed.n_filtered == 1


def test_parameter_level_elements_resolve_to_their_function():
    parsed = parse_slither_json(load("vault"), root=VULN_ROOT)
    assert [
        (f.rule_id, f.location.function, f.location.line_start, f.location.line_end)
        for f in parsed.findings
    ] == [("missing-zero-check", "setOwner", 13, 15), ("missing-zero-check", "sweep", 17, 20)]
    first = parsed.findings[0]
    # Same place as the real bug, but a different problem: must NOT count as access_control.
    assert (first.category, first.severity, first.location.contract) == ("other", Severity.LOW, "Vault")
    assert first.evidence == ["L14: owner = newOwner"]


def test_empty_results_do_not_crash():
    parsed = parse_slither_json(load("empty"), root=Path("/WORK"))
    assert (parsed.findings, parsed.n_results) == ([], 0)


def test_finding_without_a_function_uses_its_own_element():
    # Fixture not inspected when this test was written: if it fails, look at old_pragma.json.
    parsed = parse_slither_json(load("old_pragma"), root=Path("/WORK"), min_impact="informational")
    (f,) = parsed.findings
    assert (f.rule_id, f.category, f.severity) == ("solc-version", "other", Severity.INFO)
    assert f.location.function is None and f.location.line_start >= 1


# ---- synthetic cases ------------------------------------------------------------------

def element(kind="function", name="f", lines=(1, 2), file="/R/A.sol", parent=None) -> dict:
    el = {
        "type": kind,
        "name": name,
        "source_mapping": {"lines": list(lines), "filename_absolute": file, "filename_short": "A.sol"},
    }
    if parent is not None:
        el["type_specific_fields"] = {"parent": parent}
    return el


def result(check="tx-origin", impact="Medium", confidence="High", elements=None) -> dict:
    return {
        "check": check, "impact": impact, "confidence": confidence, "description": "d",
        "elements": [element()] if elements is None else elements,
    }


def doc(*results) -> dict:
    return {"success": True, "error": None, "results": {"detectors": list(results)}}


ROOT = Path("/R")


def test_statement_inside_a_function_inside_a_contract():
    contract = element("contract", "C", (1, 9))
    function = element("function", "f", (2, 5), parent=contract)
    node = element("node", "x = 1", (3,), parent=function)
    (f,) = parse_slither_json(doc(result(elements=[node])), root=ROOT).findings
    loc = f.location
    assert (loc.file, loc.line_start, loc.line_end, loc.contract, loc.function) == ("A.sol", 2, 5, "C", "f")


@pytest.mark.parametrize(
    ("threshold", "kept"),
    [("high", 1), ("medium", 2), ("low", 3), ("informational", 4)],   # "optimization" is never kept
)
def test_impact_threshold(threshold, kept):
    impacts = ["High", "Medium", "Low", "Informational", "Optimization"]
    data = doc(*[result(check=f"c{i}", impact=imp) for i, imp in enumerate(impacts)])
    parsed = parse_slither_json(data, root=ROOT, min_impact=threshold)
    assert (len(parsed.findings), parsed.n_filtered) == (kept, 5 - kept)


def test_unknown_detector_falls_back_to_other_known_one_maps():
    findings = parse_slither_json(doc(result("tx-origin"), result("brand-new-check")), root=ROOT).findings
    assert {f.rule_id: f.category for f in findings} == {
        "tx-origin": "access_control", "brand-new-check": "other",
    }


@pytest.mark.parametrize("elements", [[], [element(lines=())], [{"type": "node"}]])
def test_results_without_a_location_are_counted_not_dropped_silently(elements):
    parsed = parse_slither_json(doc(result(elements=elements)), root=ROOT)
    assert (parsed.findings, parsed.n_unusable) == ([], 1)


def test_file_outside_the_root_falls_back_to_slithers_short_name():
    (f,) = parse_slither_json(doc(result(elements=[element(file="/elsewhere/B.sol")])), root=ROOT).findings
    assert f.location.file == "A.sol"


def test_output_order_does_not_depend_on_input_order():
    a = result("tx-origin", elements=[element(lines=(1, 2))])
    b = result("suicidal", elements=[element(lines=(10, 12))])
    assert parse_slither_json(doc(a, b), root=ROOT).findings[0].rule_id == "tx-origin"
    assert parse_slither_json(doc(b, a), root=ROOT).findings[0].rule_id == "tx-origin"


@pytest.mark.parametrize("bad", [
    doc(result(impact="Catastrophic")),
    doc(result(confidence="Certain")),
    doc({"impact": "High", "confidence": "High"}),          # no check name
    {"success": True, "results": {"detectors": "nope"}},
    [],
])
def test_unexpected_shapes_raise(bad):
    with pytest.raises(SlitherParseError):
        parse_slither_json(bad, root=ROOT)


def test_failure_flag_raises_with_slithers_message():
    with pytest.raises(SlitherParseError, match="kaput"):
        parse_slither_json({"success": False, "error": "kaput", "results": {}}, root=ROOT)


def test_invalid_min_impact_is_a_programming_error():
    with pytest.raises(ValueError):
        parse_slither_json(doc(), root=ROOT, min_impact="critical")