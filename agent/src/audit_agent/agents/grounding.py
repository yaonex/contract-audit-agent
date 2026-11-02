"""Pure-function grounding post-processing for Discoverer output."""
import re

from audit_agent.context.source import RangeStatus, SourceContext
from audit_agent.schemas.finding import (
    CodeLocation,
    DiscovererOutput,
    Finding,
    FindingSource,
)
from audit_agent.schemas.report import DiscoveryInfo

MAX_DISCOVERY_FINDINGS = 10
MAX_QUOTES_PER_FINDING = 3

_LINE_PREFIX = re.compile(r"^\s*(\d+)\s*\|\s?(.*)$")
_QUOTE_PREFIX = re.compile(r"^\s*\d+\s*\|\s?")


def _clean_quote(raw_quote: str) -> str:
    """Strip line number prefixes like '14 | ' and fold whitespace."""
    lines = [_QUOTE_PREFIX.sub("", line) for line in raw_quote.splitlines()]
    return " ".join(" ".join(lines).split()).strip()


def _parse_source_lines(context_text: str) -> dict[int, str]:
    """Extract line_number -> raw_line from rendered context text."""
    lines: dict[int, str] = {}
    for line in context_text.splitlines():
        match = _LINE_PREFIX.match(line)
        if match:
            lines[int(match.group(1))] = match.group(2)
    return lines


def _verify_quotes(
    quotes: list[str],
    line_start: int,
    line_end: int,
    line_map: dict[int, str],
) -> list[str]:
    """Verify quotes against source lines in [line_start, line_end]."""
    pieces: list[str] = []
    spans: list[tuple[int, int, int]] = []
    pos = 0

    for line_no in range(line_start, line_end + 1):
        raw = line_map.get(line_no, "")
        norm = " ".join(raw.split())
        if norm:
            pieces.append(norm)
            start_pos = pos
            end_pos = pos + len(norm)
            spans.append((start_pos, end_pos, line_no))
            pos = end_pos + 1

    joined = " ".join(pieces)
    verified: list[str] = []

    for raw_quote in quotes:
        cleaned = _clean_quote(raw_quote)
        if not cleaned:
            continue

        idx = joined.find(cleaned)
        if idx != -1:
            actual_line = line_start
            for start_pos, end_pos, line_no in spans:
                if idx < end_pos:
                    actual_line = line_no
                    break
            evidence = f"L{actual_line}: {cleaned}"
            if evidence not in verified:
                verified.append(evidence)

    return verified[:MAX_QUOTES_PER_FINDING]



def ground_findings(
    output: DiscovererOutput,
    context: SourceContext,
    category: str,
    *,
    retries: int = 0,
    truncations: int = 0,
    duration_s: float = 0.0,
) -> tuple[list[Finding], DiscoveryInfo]:
    """Ground model findings against SourceContext."""
    line_map = _parse_source_lines(context.text)
    whole_source = " ".join(line_map.values())

    model_returned = len(output.findings)
    dropped_outside_file = 0
    dropped_in_omitted = 0
    dropped_invalid_range = 0
    dropped_no_verified_quote = 0
    names_cleared = 0

    valid_findings: list[Finding] = []

    for item in output.findings:
        if item.line_start > item.line_end:
            dropped_invalid_range += 1
            continue

        status = context.range_status(item.line_start, item.line_end)
        if status == RangeStatus.OUTSIDE_FILE:
            dropped_outside_file += 1
            continue
        if status == RangeStatus.IN_OMITTED:
            dropped_in_omitted += 1
            continue

        verified_evidence = _verify_quotes(
            item.quotes, item.line_start, item.line_end, line_map
        )
        if not verified_evidence:
            dropped_no_verified_quote += 1
            continue

        func_name = item.function
        if func_name and not re.search(rf"\b{re.escape(func_name)}\b", whole_source):
            func_name = None
            names_cleared += 1

        contract_name = item.contract
        if contract_name and not re.search(rf"\b{re.escape(contract_name)}\b", whole_source):
            contract_name = None
            names_cleared += 1

        finding = Finding(
            category=category,
            title=item.title,
            description=item.description,
            location=CodeLocation(
                file=context.file,
                line_start=item.line_start,
                line_end=item.line_end,
                contract=contract_name,
                function=func_name,
            ),
            evidence=verified_evidence,
            severity=item.severity,
            confidence=item.confidence,
            source=FindingSource.DISCOVERER,
            rule_id=None,
        )
        valid_findings.append(finding)

    # Dedup by fingerprint, keeping higher confidence
    dropped_duplicate = 0
    unique_map: dict[str, Finding] = {}
    for f in valid_findings:
        fp = f.fingerprint()
        if fp not in unique_map:
            unique_map[fp] = f
        else:
            dropped_duplicate += 1
            if f.confidence > unique_map[fp].confidence:
                unique_map[fp] = f

    deduped = list(unique_map.values())
    deduped.sort(key=lambda f: (-f.confidence, f.location.line_start))

    dropped_over_limit = 0
    if len(deduped) > MAX_DISCOVERY_FINDINGS:
        dropped_over_limit = len(deduped) - MAX_DISCOVERY_FINDINGS
        kept_findings = deduped[:MAX_DISCOVERY_FINDINGS]
    else:
        kept_findings = deduped

    info = DiscoveryInfo(
        model_returned=model_returned,
        kept=len(kept_findings),
        dropped_outside_file=dropped_outside_file,
        dropped_in_omitted=dropped_in_omitted,
        dropped_invalid_range=dropped_invalid_range,
        dropped_no_verified_quote=dropped_no_verified_quote,
        dropped_duplicate=dropped_duplicate,
        dropped_over_limit=dropped_over_limit,
        names_cleared=names_cleared,
        retries=retries,
        truncations=truncations,
        duration_s=duration_s,
    )

    return kept_findings, info
