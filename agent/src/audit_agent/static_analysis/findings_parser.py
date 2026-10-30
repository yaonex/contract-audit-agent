"""Slither JSON -> Finding. A PURE function: no I/O, no subprocess (see slither_runner.py).

What this relies on (observed with Slither 0.11.6; real outputs are in
tests/fixtures/slither-0.11.6/):
- Zero findings gives `"results": {}`: there is NO "detectors" key, so never index it directly.
- Each detector result has `check`, `impact` (High|Medium|Low|Informational|Optimization),
  `confidence` (High|Medium|Low), `description` and `elements`.
- An element has `type` (function, node, variable, pragma, contract, ...), `name`,
  `source_mapping.lines` (EVERY line, as a list) and `type_specific_fields.parent`, the
  enclosing element. A node/variable's parent is its function, a function's parent its contract.
- `filename_relative` depends on the directory Slither ran in, so we use `filename_absolute`
  and make it relative to the audit root.

Choices:
- Location = the enclosing FUNCTION's line range (same granularity as samples/expected.yaml
  and the usual ground-truth labels). A result whose elements are parameters or statements is
  traced up to its function; results without any function (pragma, state variable) use their
  own first element and leave `function` empty. Key statements go to `evidence`, as Slither
  renders them (not source text).
- Category comes from the taxonomy (`slither_detectors`), unmapped detectors become `other`.
  The category says WHAT kind of problem; severity says HOW bad. They are kept separate.
- confidence 0.9/0.6/0.3 for High/Medium/Low is a CONVENTION, not a calibrated probability.
  Do not use it for quantitative analysis.
- Slither never reports "critical". "Optimization" results are never kept.
- Unexpected impact/confidence values raise SlitherParseError instead of being guessed: it is
  how a Slither upgrade that changes the format becomes visible.
- Results are sorted (file, start, end, rule) so the output does not depend on Slither's order.
- Not handled yet (multi-file projects, Phase 1.4): results located in dependency files.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audit_agent.schemas.finding import CodeLocation, Finding, FindingSource, Severity
from audit_agent.schemas.taxonomy import Taxonomy, load_taxonomy

MIN_IMPACT_CHOICES = ("informational", "low", "medium", "high")

_IMPACT_RANK = {"optimization": 0, "informational": 1, "low": 2, "medium": 3, "high": 4}
_SEVERITY = {
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "informational": Severity.INFO,
}
_CONFIDENCE = {"high": 0.9, "medium": 0.6, "low": 0.3}

_MAX_EVIDENCE = 10
_MAX_EVIDENCE_CHARS = 200
_MAX_TITLE_CHARS = 200
_MAX_DESCRIPTION_CHARS = 4000


class SlitherParseError(ValueError):
    """Slither's output is not what we expect (failure flag, unknown values, wrong shape)."""


@dataclass(frozen=True)
class ParsedSlither:
    findings: list[Finding]
    n_results: int      # detector results in the file
    n_filtered: int     # dropped: below min_impact, or optimization
    n_unusable: int     # dropped: no source location to report


def parse_slither_json(
    data: Any,
    *,
    root: Path,
    min_impact: str = "low",
    taxonomy: Taxonomy | None = None,
) -> ParsedSlither:
    if min_impact not in MIN_IMPACT_CHOICES:
        raise ValueError(f"min_impact must be one of {MIN_IMPACT_CHOICES}, got {min_impact!r}")
    if not isinstance(data, dict):
        raise SlitherParseError("top level of the output is not an object")
    if data.get("success") is not True:
        raise SlitherParseError(f"slither reported failure: {data.get('error') or 'unknown error'}")

    results = data.get("results") or {}
    detectors = results.get("detectors", []) if isinstance(results, dict) else None
    if not isinstance(detectors, list):
        raise SlitherParseError("results.detectors is not a list")

    taxonomy = taxonomy or load_taxonomy()
    threshold = _IMPACT_RANK[min_impact]
    findings: list[Finding] = []
    n_filtered = n_unusable = 0

    for index, raw in enumerate(detectors):
        # Validated for every result, kept or not, so format drift is noticed early.
        check, impact, confidence = _read_header(raw, index)
        if _IMPACT_RANK[impact] < threshold:
            n_filtered += 1
            continue
        finding = _to_finding(raw, check, impact, confidence, root, taxonomy)
        if finding is None:
            n_unusable += 1
            continue
        findings.append(finding)

    findings.sort(
        key=lambda f: (f.location.file, f.location.line_start, f.location.line_end, f.rule_id or "")
    )
    return ParsedSlither(findings, len(detectors), n_filtered, n_unusable)


def _read_header(raw: Any, index: int) -> tuple[str, str, str]:
    if not isinstance(raw, dict):
        raise SlitherParseError(f"detector result #{index} is not an object")
    check = raw.get("check")
    if not isinstance(check, str) or not check:
        raise SlitherParseError(f"detector result #{index} has no 'check' name")
    impact = str(raw.get("impact", "")).lower()
    if impact not in _IMPACT_RANK:
        raise SlitherParseError(f"{check}: unexpected impact {raw.get('impact')!r}")
    confidence = str(raw.get("confidence", "")).lower()
    if confidence not in _CONFIDENCE:
        raise SlitherParseError(f"{check}: unexpected confidence {raw.get('confidence')!r}")
    return check, impact, confidence


def _lines(element: dict[str, Any]) -> list[int]:
    mapping = element.get("source_mapping")
    lines = mapping.get("lines") if isinstance(mapping, dict) else None
    if not isinstance(lines, list):
        return []
    return [n for n in lines if isinstance(n, int) and not isinstance(n, bool) and n >= 1]


def _parent(element: dict[str, Any]) -> dict[str, Any] | None:
    fields = element.get("type_specific_fields")
    parent = fields.get("parent") if isinstance(fields, dict) else None
    return parent if isinstance(parent, dict) else None


def _function_context(element: dict[str, Any]) -> dict[str, Any] | None:
    if element.get("type") == "function":
        return element
    parent = _parent(element)
    if parent is not None and parent.get("type") == "function":
        return parent
    return None


def _find_anchor(
    elements: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (element that decides the location, its function or None)."""
    for element in elements:
        function = _function_context(element)
        if function is not None and _lines(function):
            return function, function
    for element in elements:
        if _lines(element):
            return element, None
    return None, None


def _contract_name(anchor: dict[str, Any]) -> str | None:
    if anchor.get("type") == "contract":
        name = anchor.get("name")
        return str(name) if name else None
    parent = _parent(anchor)
    if parent is not None and parent.get("type") == "contract" and parent.get("name"):
        return str(parent["name"])
    return None


def _relative_file(mapping: Any, root: Path) -> str:
    mapping = mapping if isinstance(mapping, dict) else {}
    absolute = mapping.get("filename_absolute")
    if absolute:
        try:
            return Path(absolute).relative_to(root).as_posix()
        except ValueError:
            pass  # outside the audit root: fall back to what Slither calls it
    return str(mapping.get("filename_short") or mapping.get("filename_relative") or "unknown")


def _evidence(elements: list[dict[str, Any]]) -> list[str]:
    evidence: list[str] = []
    for element in elements:
        lines = _lines(element)
        name = " ".join(str(element.get("name", "")).split())
        if element.get("type") == "node" and lines and name:
            text = f"L{min(lines)}: {name}"[:_MAX_EVIDENCE_CHARS]
            if text not in evidence:
                evidence.append(text)
    return evidence[:_MAX_EVIDENCE]


def _to_finding(
    raw: dict[str, Any], check: str, impact: str, confidence: str, root: Path, taxonomy: Taxonomy
) -> Finding | None:
    elements = [e for e in (raw.get("elements") or []) if isinstance(e, dict)]
    anchor, function = _find_anchor(elements)
    if anchor is None:
        return None

    lines = _lines(anchor)
    file = _relative_file(anchor.get("source_mapping"), root)
    contract = _contract_name(anchor)
    function_name = str(function["name"]) if function is not None and function.get("name") else None
    line_start, line_end = min(lines), max(lines)

    if contract and function_name:
        where = f"{contract}.{function_name}"
    else:
        where = function_name or contract or f"{file}:L{line_start}"
    title = f"{check} in {where}"[:_MAX_TITLE_CHARS]
    description = str(raw.get("description") or "").strip()[:_MAX_DESCRIPTION_CHARS] or title

    return Finding(
        category=taxonomy.category_for_detector(check).id,
        title=title,
        description=description,
        location=CodeLocation(
            file=file,
            line_start=line_start,
            line_end=line_end,
            contract=contract,
            function=function_name,
        ),
        evidence=_evidence(elements),
        severity=_SEVERITY[impact],
        confidence=_CONFIDENCE[confidence],
        source=FindingSource.SLITHER,
        rule_id=check,
    )