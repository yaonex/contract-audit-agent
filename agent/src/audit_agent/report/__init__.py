"""Report generation and export module."""
from audit_agent.report.builder import build_report, extract_snippets
from audit_agent.report.json_export import export_report_to_json, load_report_from_json
from audit_agent.report.markdown import render_markdown
from audit_agent.report.sanitize import sanitize_markdown, sanitize_markdown_inline
from audit_agent.report.writer import write_report

__all__ = [
    "build_report",
    "export_report_to_json",
    "extract_snippets",
    "load_report_from_json",
    "render_markdown",
    "sanitize_markdown",
    "sanitize_markdown_inline",
    "write_report",
]

