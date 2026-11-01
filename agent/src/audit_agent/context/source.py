"""Source context: a contract file as numbered text for the prompt, plus what the model saw.

LINE NUMBERS are a contract between three parties: the model's citations, Slither/solc, and the
labels of the evaluation data. If we count lines differently, matching is shifted by one.
Rules:
- Decode as UTF-8 and drop a BOM. Invalid bytes are replaced (line numbers are unaffected) and
  flagged in the result.
- Normalise "\\r\\n" to "\\n", then split ONLY on "\\n". Never str.splitlines(): it also breaks on
  form feed, \\x85, \\u2028 ... which would put our numbers ahead of the compiler's. (How solc
  counts is not documented here: tests reconcile with real Slither output instead.)
- A final newline does not start a line: "a\\nb\\n" and "a\\nb" both have 2 lines. (Editors such
  as VS Code display an extra EMPTY last line for the first; nothing refers to it.)
- Lone "\\r" (very old Mac files) is not handled; the reconciliation tests would show it.
- Empty lines are numbered like every other line. Indentation is kept as is.

FORMAT: `<number right-aligned> | <line>`, e.g. `11 |     function withdraw() external {`.
An empty line is `3 |` (no trailing space). The width depends on the total line count. Every
code line carries the prefix, so source text cannot easily pose as anything else in the prompt
(a line of ``` inside the contract becomes `12 | ```` and cannot close a code block). That does
NOT stop semantic injection ("ignore previous instructions" in a comment): the prompt must say
that everything in the code block is data (step 4).

COVERAGE is never hidden. If the file does not fit, whole lines are left out and replaced by a
marker (see budget.py); the result says exactly which ranges the model saw. A single line
longer than MAX_LINE_CHARS is shortened with a visible note. imports are only DETECTED
(best effort, comments removed first, not a parser); imported files are NOT included.

range_status() says whether a cited line range is real AND was shown. A range the model never
saw is invented, whatever the model claims. What to do with it is the caller's decision.
clamp() corrects Slither's habit of ending a contract-level range one line past the end of a
file that has no final newline (observed, see docs/pitfalls.md); it does NOT repair ranges that
lie completely outside the file (returns None).

Determinism: the text never contains absolute paths or timestamps; the file name is relative
to the audit root. This keeps response-cache keys stable, so replay mode can hit.
"""
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from audit_agent.context.budget import select_lines
from audit_agent.schemas.report import ContextInfo

MAX_FILE_BYTES = 2 * 1024 * 1024      # a guess; refuse bigger files instead of reading them whole
MAX_LINE_CHARS = 2000
_MAX_IMPORTS = 50
_MAX_IMPORT_CHARS = 200

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_IMPORT = re.compile(r"(?:^|(?<=;))\s*(import\b[^;]*;)", re.M)


class RangeStatus(StrEnum):
    OK = "ok"                      # inside the file and fully shown to the model
    OUTSIDE_FILE = "outside_file"  # not a valid range of this file
    IN_OMITTED = "in_omitted"      # at least one line was never shown to the model


@dataclass(frozen=True)
class SourceContext:
    file: str                                        # relative to the audit root
    text: str                                        # numbered, ready for the prompt
    total_lines: int
    included_ranges: tuple[tuple[int, int], ...]     # 1-based inclusive
    est_tokens: int
    max_tokens: int
    n_long_lines: int
    imports: tuple[str, ...]
    had_decode_errors: bool

    @property
    def truncated(self) -> bool:
        return sum(end - start + 1 for start, end in self.included_ranges) < self.total_lines

    @property
    def omitted_ranges(self) -> tuple[tuple[int, int], ...]:
        gaps: list[tuple[int, int]] = []
        previous_end = 0
        for start, end in self.included_ranges:
            if start > previous_end + 1:
                gaps.append((previous_end + 1, start - 1))
            previous_end = end
        if previous_end < self.total_lines:
            gaps.append((previous_end + 1, self.total_lines))
        return tuple(gaps)

    def range_status(self, start: int, end: int) -> RangeStatus:
        if start < 1 or end < start or end > self.total_lines:
            return RangeStatus.OUTSIDE_FILE
        for hidden_start, hidden_end in self.omitted_ranges:
            if start <= hidden_end and end >= hidden_start:
                return RangeStatus.IN_OMITTED
        return RangeStatus.OK

    def clamp(self, start: int, end: int) -> tuple[int, int] | None:
        """Cut a range back to the file. None if no line of it lies inside the file."""
        if end < start:
            raise ValueError(f"invalid range {start}-{end}")
        if end < 1 or start > self.total_lines:
            return None
        return max(start, 1), min(end, self.total_lines)

    def to_info(self) -> ContextInfo:
        return ContextInfo(
            file=self.file,
            total_lines=self.total_lines,
            included_ranges=list(self.included_ranges),
            truncated=self.truncated,
            est_tokens=self.est_tokens,
            max_tokens=self.max_tokens,
            n_long_lines=self.n_long_lines,
            imports=list(self.imports),
            had_decode_errors=self.had_decode_errors,
        )


def split_lines(text: str) -> list[str]:
    """Split like a compiler: only on "\\n" (after "\\r\\n" became "\\n"); a final newline
    does not start another line."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def find_imports(text: str) -> tuple[str, ...]:
    """Import statements, comments removed first. Best effort, not a parser."""
    code = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))
    found: list[str] = []
    for match in _IMPORT.finditer(code):
        statement = " ".join(match.group(1).split())[:_MAX_IMPORT_CHARS]
        if statement not in found:
            found.append(statement)
    return tuple(found[:_MAX_IMPORTS])


def _render(lines: list[str], max_line_chars: int) -> tuple[list[str], int]:
    width = len(str(len(lines)))
    rendered: list[str] = []
    n_long = 0
    for number, line in enumerate(lines, start=1):
        if len(line) > max_line_chars:
            n_long += 1
            line = f"{line[:max_line_chars]} [... {len(line) - max_line_chars} more chars]"
        rendered.append(f"{number:>{width}} | {line}" if line else f"{number:>{width}} |")
    return rendered, n_long


def build_context_from_text(
    text: str,
    *,
    file: str,
    max_tokens: int = 20_000,
    max_line_chars: int = MAX_LINE_CHARS,
    had_decode_errors: bool = False,
) -> SourceContext:
    if not text.strip():
        raise ValueError("source is empty: nothing to audit")
    if max_line_chars < 1:
        raise ValueError("max_line_chars must be >= 1")
    lines = split_lines(text)
    rendered, n_long = _render(lines, max_line_chars)
    selection = select_lines(rendered, max_tokens)
    return SourceContext(
        file=file,
        text=selection.text,
        total_lines=len(lines),
        included_ranges=selection.included,
        est_tokens=selection.est_tokens,
        max_tokens=max_tokens,
        n_long_lines=n_long,
        imports=find_imports(text),
        had_decode_errors=had_decode_errors,
    )


def read_source_text(path: Path) -> tuple[str, bool]:
    """Decoded text and whether invalid bytes had to be replaced."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_FILE_BYTES} bytes: refusing to read it")
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        return data.decode("utf-8-sig", errors="replace"), True


def _display_name(path: Path, root: Path | None) -> str:
    resolved = path.resolve()
    if root is not None:
        try:
            return resolved.relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass    # outside the root: the bare name, never an absolute path
    return resolved.name


def build_context(
    path: Path,
    *,
    root: Path | None = None,
    max_tokens: int = 20_000,
    max_line_chars: int = MAX_LINE_CHARS,
) -> SourceContext:
    """Read `path` (one .sol file). Raises FileNotFoundError if it does not exist (the caller's
    mistake, as in slither_runner) and ValueError for a directory, an empty or oversized file,
    or a budget below the minimum."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"audit target not found: {path}")
    if path.is_dir():
        raise ValueError(f"{path} is a directory: only single files are supported for now")
    text, had_errors = read_source_text(path)
    return build_context_from_text(
        text,
        file=_display_name(path, root),
        max_tokens=max_tokens,
        max_line_chars=max_line_chars,
        had_decode_errors=had_errors,
    )