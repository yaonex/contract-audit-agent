"""JSON exporter and loader for AuditReport."""
from audit_agent.schemas.report import AuditReport


def export_report_to_json(report: AuditReport, *, indent: int = 2) -> str:
    """Serialize an AuditReport object to a JSON string."""
    return report.model_dump_json(indent=indent)


def load_report_from_json(data: str | bytes) -> AuditReport:
    """Deserialize JSON data into an AuditReport object."""
    return AuditReport.model_validate_json(data)

