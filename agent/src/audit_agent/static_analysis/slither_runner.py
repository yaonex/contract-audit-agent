"""Run Slither as a subprocess and turn the outcome into a StaticAnalysisResult.

Where the tools come from:
- slither-analyzer and solc-select are PROJECT dependencies (pyproject, pinned), so the Slither
  version is in uv.lock and in every report. Compiler binaries are not Python packages: run
  `make setup-solc` once (and again after the virtualenv is recreated); it downloads the
  version in AUDIT_SOLC_VERSION.
- solc-select keeps its state (the selected version, the downloaded compilers) under
  $VIRTUAL_ENV/.solc-select when VIRTUAL_ENV is set, and under ~/.solc-select otherwise
  (constants.py of solc-select 1.2.0; internal behaviour, not documented API). So one machine
  could see two different states depending on HOW the process was started. That was the cause
  of "No solc version set" under `uv run`. The runner therefore pins VIRTUAL_ENV and PATH to
  the environment of the running interpreter, so any way of starting the CLI behaves the same.
  tests/integration/test_slither_real.py::test_works_without_uv_run_around_it guards this: it
  will fail if a solc-select upgrade changes the rule.

Facts about Slither this is built on (0.11.6; real outputs in tests/fixtures/slither-0.11.6/):
- Without `--fail-none` Slither exits 255 when it FOUND something. With it, exit 0 means "the
  analysis ran" and anything else means it did not.
- On a compile error (syntax error, pragma not matching the installed solc) NO json file is
  written; the reason is a Python traceback on stderr that contains "InvalidCompilation".
- If solc itself is unusable (no version selected, version not installed) Slither ALSO reports
  InvalidCompilation. classify_failure() tells the two apart: "your contract is broken" and
  "the environment is not set up" need different messages.
- Zero findings: a json with `"results": {}`.
- OK = exit code 0 AND a json file AND it parses AND `"success": true`. Anything else is a
  failure and the pipeline degrades: the audit goes on without static analysis, and the report
  records the status. (A missing target file is the caller's bug, so that raises instead.)

Design notes:
- Always `--compile-force-framework solc`: automatic detection depends on files lying around in
  the audited directory, and my understanding (not verified) is that other frameworks make
  crytic-compile run the project's own build tool. Forcing solc is stable and the safer choice.
- The target is passed as an absolute path (it can never be read as an option) and the process
  runs in a scratch directory, so a stray slither.config.json in the current directory cannot
  change the behaviour.
- The process runs in its own session and a timeout kills the whole group; killing only Slither
  could leave its solc child behind. POSIX only (fine on WSL/Linux).
- The solc version comes from the `solc_version` argument (per-contract choice, Phase 1.3), else
  from settings.solc_version, else from solc-select's selection in the project environment.
- Single files only for now. Directory targets are passed through but not verified.
- Tool versions are recorded: detectors change between versions, so results are only comparable
  when the versions match.
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from audit_agent.config import Settings
from audit_agent.observability.tracer import SpanHandle, Tracer
from audit_agent.schemas.finding import Finding
from audit_agent.schemas.report import StaticAnalysisInfo, StaticAnalysisStatus
from audit_agent.schemas.taxonomy import Taxonomy
from audit_agent.static_analysis.findings_parser import (
    ParsedSlither,
    SlitherParseError,
    parse_slither_json,
)

logger = logging.getLogger(__name__)

_MAX_ERROR_CHARS = 1500
_TAIL_LINES = 15
_VERSION_TIMEOUT_S = 15.0
_SLITHER_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\S*)")
_SOLC_VERSION_RE = re.compile(r"Version:\s*(\d+\.\d+\.\d+(?:\+commit\.[0-9a-f]+)?)")

# Texts (taken from real output and from solc-select's source) that mean "solc itself is not
# usable", as opposed to "the contract does not compile". Slither wraps both in
# InvalidCompilation, so these are checked first.
_SOLC_UNUSABLE_MARKERS = (
    "Solidity version not found",
    "No solc version set",
    "not installed (set by",
)

S = StaticAnalysisStatus


class StaticAnalysisResult(BaseModel):
    info: StaticAnalysisInfo
    findings: list[Finding] = Field(default_factory=list)
    tool_versions: dict[str, str] = Field(default_factory=dict)


@dataclass
class _Outcome:
    status: StaticAnalysisStatus
    error: str | None = None
    parsed: ParsedSlither | None = None
    raw_json: str | None = None
    stderr: str = ""


# ---- text helpers ---------------------------------------------------------------------

def _cap(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _tail(text: str, lines: int = _TAIL_LINES) -> str:
    kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return _cap("\n".join(kept)) if kept else "(no output)"


def extract_compile_error(stderr: str) -> str:
    """The useful part of Slither's stderr for a compile failure: solc's own error block,
    without the Python traceback around it."""
    marker = "Invalid solc compilation "
    if marker in stderr:
        text = stderr.rsplit(marker, 1)[1]
    else:
        match = re.search(r"^Error.*$", stderr, re.M)
        text = stderr[match.start():] if match else _tail(stderr)
    return _cap(re.sub(r"\n{3,}", "\n\n", text))


def classify_failure(returncode: int, stdout: str, stderr: str) -> tuple[StaticAnalysisStatus, str]:
    """Slither produced no json. Work out why, from the text it printed."""
    text = stderr if stderr.strip() else stdout
    if any(marker in text for marker in _SOLC_UNUSABLE_MARKERS):
        return S.TOOL_MISSING, (
            "solc is not usable: no version is selected, or the selected one is not installed. "
            "Run `make setup-solc` (installs AUDIT_SOLC_VERSION into the project environment). "
            f"Last lines: {_tail(text, 3)}"
        )
    if "InvalidCompilation" in text or "Invalid solc compilation" in text:
        return S.COMPILE_FAILED, extract_compile_error(text)
    return S.FAILED, f"slither exited with code {returncode}: {_tail(text)}"


# ---- which tool, which environment ----------------------------------------------------

def _env_bin_dir() -> Path | None:
    """The bin directory of the virtualenv this interpreter runs in; None outside one.
    Deliberately NOT resolved: sys.executable inside a venv is a symlink to the base python."""
    if sys.prefix == sys.base_prefix:
        return None
    return Path(sys.prefix) / "bin"


def resolve_slither(configured: str) -> str:
    """The command used to start Slither.

    A configured value containing "/" is used as given. A bare name is looked up in the
    virtualenv of the running interpreter first (so we run the pinned project copy whatever
    PATH looks like), then left to the normal PATH lookup.
    """
    if "/" in configured:
        return configured
    bin_dir = _env_bin_dir()
    if bin_dir is not None and (bin_dir / configured).is_file():
        return str(bin_dir / configured)
    return configured


def _build_env(solc_version: str | None) -> dict[str, str]:
    env = dict(os.environ)
    bin_dir = _env_bin_dir()
    if bin_dir is not None:
        # See the module docstring: this decides where solc-select looks for its state, and
        # puts the project's solc (the solc-select forwarder) ahead of any other.
        env["VIRTUAL_ENV"] = sys.prefix
        env["PATH"] = os.pathsep.join(p for p in (str(bin_dir), env.get("PATH")) if p)
    if solc_version:
        env["SOLC_VERSION"] = solc_version
    return env


# ---- subprocess helpers ---------------------------------------------------------------

def _run_process(
    cmd: list[str], *, env: dict[str, str], cwd: str | None, timeout_s: float
) -> tuple[int, str, str, bool]:
    """Returns (returncode, stdout, stderr, timed_out). Raises OSError if it cannot start."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
        env=env, cwd=cwd, start_new_session=True,    # own process group, so we can kill all of it
    )
    try:
        out, err = proc.communicate(timeout=timeout_s)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        return proc.returncode, out or "", err or "", True


_version_cache: dict[tuple[tuple[str, ...], str | None], str] = {}


def _probe_version(
    cmd: list[str], pattern: re.Pattern[str], env: dict[str, str], solc_version: str | None
) -> str:
    """Version string of an external tool, or "unknown". Cached per command: an evaluation
    runs hundreds of audits and the version does not change in between."""
    key = (tuple(cmd), solc_version)
    if key in _version_cache:
        return _version_cache[key]
    try:
        _, out, err, timed_out = _run_process(cmd, env=env, cwd=None, timeout_s=_VERSION_TIMEOUT_S)
    except OSError:
        return "unknown"
    match = None if timed_out else pattern.search(f"{out}\n{err}")
    version = match.group(1) if match else "unknown"
    if version != "unknown":
        _version_cache[key] = version
    return version


# ---- the run --------------------------------------------------------------------------

def _execute(
    cmd: list[str],
    *,
    env: dict[str, str],
    scratch: str,
    json_path: Path,
    timeout_s: float,
    root: Path,
    min_impact: str,
    taxonomy: Taxonomy | None,
) -> _Outcome:
    try:
        code, out, err, timed_out = _run_process(cmd, env=env, cwd=scratch, timeout_s=timeout_s)
    except OSError as exc:
        return _Outcome(
            S.TOOL_MISSING,
            f"cannot start '{cmd[0]}': {exc}. Slither is a project dependency: run `make sync`, "
            "or set AUDIT_SLITHER_PATH.",
        )
    if timed_out:
        return _Outcome(
            S.TIMEOUT, f"slither did not finish within {timeout_s:g}s and was killed", stderr=err
        )

    if not json_path.exists():
        status, message = classify_failure(code, out, err)
        return _Outcome(status, message, stderr=err or out)

    raw_json = json_path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return _Outcome(S.FAILED, f"slither wrote unreadable JSON ({exc.msg})", raw_json=raw_json)
    try:
        parsed = parse_slither_json(data, root=root, min_impact=min_impact, taxonomy=taxonomy)
    except SlitherParseError as exc:
        return _Outcome(S.FAILED, f"unexpected slither output: {exc}", raw_json=raw_json)
    if code != 0:
        return _Outcome(
            S.FAILED,
            f"slither exited with code {code} although it wrote JSON: {_tail(err or out)}",
            raw_json=raw_json,
        )
    return _Outcome(S.OK, parsed=parsed, raw_json=raw_json)


def run_slither(
    target: Path,
    *,
    settings: Settings,
    tracer: Tracer | None = None,
    solc_version: str | None = None,
    root: Path | None = None,
    taxonomy: Taxonomy | None = None,
) -> StaticAnalysisResult:
    """Analyse `target` (a .sol file) and return findings plus a status.

    Never raises for problems of the analysis itself (see the module docstring); raises
    FileNotFoundError when `target` does not exist. `root` is what finding paths are made
    relative to (default: the file's directory, so a file is reported as "Bank.sol").
    """
    target_path = Path(target).resolve()
    if not target_path.exists():
        raise FileNotFoundError(f"audit target not found: {target_path}")
    audit_root = (
        Path(root).resolve()
        if root is not None
        else (target_path if target_path.is_dir() else target_path.parent)
    )

    # An explicit argument (per-contract choice, Phase 1.3) beats the configured default;
    # an empty string counts as "not set".
    solc_version = solc_version or settings.solc_version or None

    slither_cmd = resolve_slither(settings.slither_path)
    env = _build_env(solc_version)
    versions = {
        "slither": _probe_version([slither_cmd, "--version"], _SLITHER_VERSION_RE, env, solc_version),
        "solc": _probe_version(["solc", "--version"], _SOLC_VERSION_RE, env, solc_version),
    }

    span = (
        tracer.span("tool", "slither", target=str(target_path))
        if tracer
        else nullcontext(SpanHandle())
    )
    with span as sp, tempfile.TemporaryDirectory(prefix="audit-slither-") as scratch:
        json_path = Path(scratch) / "slither.json"
        cmd = [
            slither_cmd, str(target_path),
            "--compile-force-framework", "solc",
            "--json", str(json_path),
            "--fail-none",
        ]
        started = time.perf_counter()
        outcome = _execute(
            cmd, env=env, scratch=scratch, json_path=json_path,
            timeout_s=settings.slither_timeout_s, root=audit_root,
            min_impact=settings.slither_min_impact, taxonomy=taxonomy,
        )
        duration = round(time.perf_counter() - started, 3)

        raw_path: str | None = None
        if tracer is not None:
            if outcome.raw_json is not None:
                raw_path = tracer.write_blob("slither_json", outcome.raw_json)
            elif outcome.stderr:
                raw_path = tracer.write_blob("slither_stderr", outcome.stderr)

        parsed = outcome.parsed
        info = StaticAnalysisInfo(
            status=outcome.status,
            error=_cap(outcome.error) if outcome.error else None,
            n_results=parsed.n_results if parsed else 0,
            n_kept=len(parsed.findings) if parsed else 0,
            n_filtered=parsed.n_filtered if parsed else 0,
            n_unusable=parsed.n_unusable if parsed else 0,
            min_impact=settings.slither_min_impact,
            duration_s=duration,
            raw_output_path=raw_path,
        )
        sp.set(status=info.status.value, n_results=info.n_results, n_kept=info.n_kept)
        if outcome.status != S.OK:
            sp.fail(f"{outcome.status.value}: {info.error}")
            logger.warning("static analysis %s: %s", outcome.status.value, info.error)

        return StaticAnalysisResult(
            info=info,
            findings=list(parsed.findings) if parsed else [],
            tool_versions=versions,
        )