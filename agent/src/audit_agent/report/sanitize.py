"""Markdown and HTML safety sanitization for untrusted LLM-generated text.

Jinja2 autoescape only escapes HTML (&, <, >, ", '). It does NOT prevent:
- Code fences (``` or ~~~) closing early and injecting raw markdown.
- Headings (#, ##) injecting unexpected top-level sections into the report.
- List markers (-, *, +, 1.) breaking table cells or list structures.
- Raw HTML tags executing in browsers or breaking markdown parsers.
"""
import html
import re


def sanitize_markdown(text: str | None) -> str:
    """Sanitize multi-line markdown text (e.g., description, exploit_path, recommendation).

    Preserves paragraph structure and line breaks, but defangs markdown formatting
    injections and HTML tags.
    """
    if not text:
        return ""

    # 1. Escape HTML characters (<, >, &)
    sanitized = html.escape(text, quote=False)

    # 2. Defang markdown code fences: 3 or more backticks or tildes
    sanitized = re.sub(r"`{3,}", lambda m: "\\" + "\\".join(m.group(0)), sanitized)
    sanitized = re.sub(r"~{3,}", lambda m: "\\" + "\\".join(m.group(0)), sanitized)

    # 3. Defang headings at line start (#, ##, ###, etc.)
    sanitized = re.sub(r"(?m)^(\s*)(#{1,6})(\s+)", r"\1\\\2\3", sanitized)

    # 4. Defang list markers at line start (-, *, +, 1., 2., etc.)
    sanitized = re.sub(r"(?m)^(\s*)([-*+])(\s+)", r"\1\\\2\3", sanitized)
    sanitized = re.sub(r"(?m)^(\s*)(\d+)\.(\s+)", r"\1\2\.\3", sanitized)

    return sanitized


def sanitize_markdown_inline(text: str | None) -> str:
    """Sanitize single-line text (e.g., titles, table cell contents).

    Replaces newlines with spaces and escapes pipes (|) to prevent table layout corruption.
    """
    if not text:
        return ""

    # Flatten newlines
    flattened = " ".join(text.splitlines())
    # Escape pipe characters for markdown table integrity
    escaped_pipe = flattened.replace("|", "\\|")
    return sanitize_markdown(escaped_pipe)
