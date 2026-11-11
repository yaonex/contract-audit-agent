"""Markdown report renderer.

Pure function: takes an AuditReport and returns formatted Markdown.
Zero disk I/O during rendering: template is loaded into memory at import time.
All sanitization, grouping, and metrics formatting are calculated in Python before templating.
"""
from pathlib import Path
from typing import Any

from jinja2 import Environment

from audit_agent.report.sanitize import sanitize_markdown, sanitize_markdown_inline
from audit_agent.schemas.report import AuditReport, StaticAnalysisStatus

# Read and compile the Jinja2 template once at module load time
_TEMPLATE_FILE = Path(__file__).parent / "templates" / "report.md.j2"
_TEMPLATE_SOURCE = _TEMPLATE_FILE.read_text(encoding="utf-8")
_ENV = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
_TEMPLATE = _ENV.from_string(_TEMPLATE_SOURCE)


def _prepare_template_context(report: AuditReport) -> dict[str, Any]:
    """Prepare sanitized, pre-computed data for the Jinja2 template."""
    # Meta summary
    target_path = sanitize_markdown_inline(report.meta.target_path or report.target or "unknown")
    target_hash = report.meta.target_hash or "N/A"
    gen_time = report.meta.generated_at.strftime("%Y-%m-%d %H:%M:%S")

    components: list[str] = [
        f"RAG: {'Enabled' if report.run_config.rag_enabled else 'Disabled'}",
        f"Verifier: {'Enabled' if report.run_config.verifier_enabled else 'Disabled'}",
        f"Judge: {'Enabled' if report.run_config.judge_enabled else 'Disabled'}",
    ]
    components_summary = " | ".join(components)

    tool_versions_summary = ", ".join(
        f"{sanitize_markdown_inline(k)} {sanitize_markdown_inline(v)}"
        for k, v in sorted(report.meta.tool_versions.items())
    )

    meta_dict = {
        "target_path": target_path,
        "target_hash": target_hash,
        "generated_at": gen_time,
        "components_summary": components_summary,
        "tool_versions_summary": tool_versions_summary,
    }

    # Degradations
    degradations_items = []
    for d in report.degradations:
        degradations_items.append({
            "stage": sanitize_markdown_inline(d.stage),
            "type": d.type.value,
            "description": sanitize_markdown_inline(d.description),
            "location": sanitize_markdown_inline(d.location or "N/A"),
        })

    degradations_dict = {
        "has_degradations": len(report.degradations) > 0,
        "records": degradations_items,
    }

    # Severity counts
    counts = report.counts_by_severity()
    severity_counts = {
        "critical": counts.get("critical", 0),
        "high": counts.get("high", 0),
        "medium": counts.get("medium", 0),
        "low": counts.get("low", 0),
        "info": counts.get("info", 0),
        "total": len(report.findings),
    }

    # Zero findings status
    zero_status = None
    if len(report.findings) == 0:
        if len(report.degradations) > 0:
            zero_status = (
                "> ⚠️ **Warning: Zero Findings with Degradations**  \n"
                "> No vulnerabilities were identified, but execution encountered system degradations "
                "(see Degradation Log above). This cannot be interpreted as a clean audit result."
            )
        else:
            zero_status = (
                "> ✅ **Clean Result: Zero Findings**  \n"
                "> The audit completed successfully with full coverage and no degradations. "
                "No vulnerabilities were detected under the covered scope (reentrancy)."
            )

    # Findings detail
    findings_list = []
    for idx, f in enumerate(report.findings, start=1):
        where = f"{f.location.contract or ''}.{f.location.function or ''}".strip(".")
        loc_str = f"{f.location.file}:{f.location.line_start}-{f.location.line_end}"
        if where:
            loc_str += f" ({where})"

        snippets_rendered = [s.render() for s in f.snippets if s.lines]

        findings_list.append({
            "index": idx,
            "id": f.id,
            "title": sanitize_markdown_inline(f.title),
            "category": sanitize_markdown_inline(f.category),
            "severity": f.severity.value.upper(),
            "severity_source": sanitize_markdown_inline(f.severity_source),
            "confidence": f"{f.confidence:.2f}",
            "confidence_source": sanitize_markdown_inline(f.confidence_source),
            "status": f.status.value,
            "location_str": sanitize_markdown_inline(loc_str),
            "rule_id": sanitize_markdown_inline(f.rule_id) if f.rule_id else None,
            "description": sanitize_markdown(f.description),
            "exploit_path": sanitize_markdown(f.exploit_path) if f.exploit_path else None,
            "recommendation": sanitize_markdown(f.recommendation) if f.recommendation else None,
            "snippets": snippets_rendered,
        })

    # Appendix: funnel, stats, run_config
    if report.static_analysis.status == StaticAnalysisStatus.NOT_RUN:
        slither_display = "N/A (not run)"
    elif report.static_analysis.status != StaticAnalysisStatus.OK:
        err_msg = f": {report.static_analysis.error}" if report.static_analysis.error else ""
        slither_display = f"Failed ({report.static_analysis.status.value}{err_msg})"
    else:
        cnt = report.stats.slither_findings if report.stats.slither_findings is not None else 0
        slither_display = str(cnt)

    funnel_dict = {
        "slither_findings": slither_display,
        "discoverer_candidates": str(report.stats.discoverer_candidates),
        "final_findings": str(report.stats.final_findings),
    }

    stats_dict = {
        "llm_calls": report.stats.llm_calls,
        "validation_retries": report.stats.validation_retries,
        "prompt_tokens": report.stats.prompt_tokens,
        "completion_tokens": report.stats.completion_tokens,
        "total_tokens": report.stats.total_tokens,
        "cost_usd": report.stats.cost_usd,
        "duration_s": f"{report.stats.duration_s:.2f}",
    }

    run_config_dict = {
        "model": sanitize_markdown_inline(report.run_config.model),
        "provider": sanitize_markdown_inline(report.run_config.provider or "N/A"),
        "prompt_version": sanitize_markdown_inline(report.run_config.prompt_version),
        "max_total_tokens": report.run_config.max_total_tokens,
        "max_cost_usd": report.run_config.max_cost_usd,
        "rag_enabled": report.run_config.rag_enabled,
        "verifier_enabled": report.run_config.verifier_enabled,
        "judge_enabled": report.run_config.judge_enabled,
        "covered_categories": [
            sanitize_markdown_inline(c) for c in report.run_config.covered_categories
        ],
    }

    return {
        "meta": meta_dict,
        "run_config": run_config_dict,
        "degradations": degradations_dict,
        "severity_counts": severity_counts,
        "zero_findings_status": zero_status,
        "findings": findings_list,
        "funnel": funnel_dict,
        "stats": stats_dict,
    }


def render_markdown(report: AuditReport) -> str:
    """Render an AuditReport to Markdown text.

    Pure function: no network, no LLM, no disk access.
    """
    ctx = _prepare_template_context(report)
    return _TEMPLATE.render(**ctx)

