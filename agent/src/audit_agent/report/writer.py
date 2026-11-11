"""File writing entrypoint for audit reports (JSON and Markdown)."""
from pathlib import Path

from audit_agent.config import Settings, get_settings
from audit_agent.report.json_export import export_report_to_json
from audit_agent.report.markdown import render_markdown
from audit_agent.schemas.report import AuditReport


def write_report(
    report: AuditReport,
    output_dir: Path | str | None = None,
    *,
    filename_prefix: str | None = None,
    settings: Settings | None = None,
) -> tuple[Path, Path]:
    """Write an AuditReport as both JSON and Markdown files to the target directory.

    Defaults to settings.report_dir ('reports/').
    Returns (json_path, md_path).
    """
    cfg = settings or get_settings()
    out_dir = Path(output_dir) if output_dir is not None else cfg.report_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = report.meta.generated_at.strftime("%Y%m%d_%H%M%S")
    if filename_prefix:
        base_name = f"{filename_prefix}_{timestamp}"
    else:
        target_name = Path(report.meta.target_path or report.target or "contract").stem
        base_name = f"audit_{target_name}_{timestamp}"

    json_path = out_dir / f"{base_name}.json"
    md_path = out_dir / f"{base_name}.md"

    json_content = export_report_to_json(report, indent=2)
    md_content = render_markdown(report)

    json_path.write_text(json_content, encoding="utf-8")
    md_path.write_text(md_content, encoding="utf-8")

    return json_path, md_path
