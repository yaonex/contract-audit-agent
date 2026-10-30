import json
import os
import sys
import time
from pathlib import Path

import pytest

from audit_agent.config import Settings
from audit_agent.observability.tracer import Tracer, read_events
from audit_agent.schemas.report import StaticAnalysisStatus as S
from audit_agent.static_analysis.slither_runner import (
    classify_failure,
    extract_compile_error,
    resolve_slither,
    run_slither,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "slither-0.11.6"
ROOT = Path("/ROOT/samples/vulnerable")

_BODY = '''
import json, os, shutil, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("0.11.6")
    sys.exit(0)
with open(CONFIG["RECORD"], "w", encoding="utf-8") as f:
    json.dump({
        "args": args,
        "solc_version": os.environ.get("SOLC_VERSION"),
        "virtual_env": os.environ.get("VIRTUAL_ENV"),
        "path": os.environ.get("PATH"),
        "cwd": os.getcwd(),
    }, f)
time.sleep(CONFIG["SLEEP"])
out = args[args.index("--json") + 1] if "--json" in args else None
if out and CONFIG["JSON_SRC"]:
    shutil.copy(CONFIG["JSON_SRC"], out)
if out and CONFIG["JSON_TEXT"] is not None:
    open(out, "w", encoding="utf-8").write(CONFIG["JSON_TEXT"])
if CONFIG["STDERR_SRC"]:
    sys.stderr.write(open(CONFIG["STDERR_SRC"], encoding="utf-8").read())
if CONFIG["STDERR_TEXT"]:
    sys.stderr.write(CONFIG["STDERR_TEXT"])
sys.exit(CONFIG["EXIT"])
'''

# Real texts: the first two are what Slither printed in the failing integration runs that
# motivated the "environment is not set up" classification (see docs/pitfalls.md).
_UNCONFIGURED_SOLC = (
    "crytic_compile.platform.exceptions.InvalidCompilation:\n"
    "Solidity version not found:\nSTDOUT:\nSTDERR:\nTraceback (most recent call last):\n"
    "argparse.ArgumentTypeError: No solc version set. Run `solc-select use VERSION` "
    "or set SOLC_VERSION environment variable.\n"
)
_NOT_INSTALLED_SOLC = (
    "crytic_compile.platform.exceptions.InvalidCompilation:\n"
    "Solidity version not found:\nSTDOUT:\nSTDERR:\nTraceback (most recent call last):\n"
    "argparse.ArgumentTypeError: \nVersion '0.8.34' not installed (set by SOLC_VERSION).\n"
    "Run `solc-select install 0.8.34`.\n"
)


def make_fake_slither(
    tmp_path, *, json_name=None, json_text=None, stderr_name=None, stderr_text="",
    exit_code=0, sleep_s=0.0,
):
    """A stand-in executable that behaves like Slither: records how it was called, writes a
    real captured json (or none) and a real captured stderr, exits with a chosen code."""
    record = tmp_path / "record.json"
    config = {
        "RECORD": str(record),
        "SLEEP": sleep_s,
        "EXIT": exit_code,
        "JSON_SRC": str(FIX / f"{json_name}.json") if json_name else "",
        "JSON_TEXT": json_text,
        "STDERR_SRC": str(FIX / f"{stderr_name}.stderr.txt") if stderr_name else "",
        "STDERR_TEXT": stderr_text,
    }
    script = tmp_path / "fake_slither"
    script.write_text(f"#!{sys.executable}\nCONFIG = {config!r}\n{_BODY}", encoding="utf-8")
    script.chmod(0o755)
    return script, record


def settings(script, **over) -> Settings:
    return Settings(_env_file=None, slither_path=str(script), **over)  # type: ignore[call-arg]


def target(tmp_path) -> Path:
    path = tmp_path / "Bank.sol"
    path.write_text("// placeholder", encoding="utf-8")
    return path


def recorded(record: Path) -> dict:
    return json.loads(record.read_text(encoding="utf-8"))


# ---- success --------------------------------------------------------------------------

def test_success_parses_findings_and_passes_the_expected_arguments(tmp_path):
    script, record = make_fake_slither(tmp_path, json_name="bank_vulnerable")
    t = target(tmp_path)
    res = run_slither(t, settings=settings(script), root=ROOT)
    assert (res.info.status, res.info.error) == (S.OK, None)
    assert [f.rule_id for f in res.findings] == ["reentrancy-eth"]
    assert (res.info.n_results, res.info.n_kept, res.info.n_filtered) == (2, 1, 1)
    assert res.tool_versions["slither"] == "0.11.6" and "solc" in res.tool_versions

    rec = recorded(record)
    args = rec["args"]
    assert args[0] == str(t.resolve())                              # absolute target, first
    assert args[args.index("--compile-force-framework") + 1] == "solc"
    assert "--fail-none" in args
    assert args[args.index("--json") + 1].endswith("slither.json")
    assert "audit-slither-" in rec["cwd"]                           # ran in its own scratch dir


def test_zero_findings_is_ok(tmp_path):
    script, _ = make_fake_slither(tmp_path, json_name="empty")
    res = run_slither(target(tmp_path), settings=settings(script))
    assert (res.info.status, res.findings, res.info.n_results) == (S.OK, [], 0)


def test_min_impact_setting_is_applied(tmp_path):
    script, _ = make_fake_slither(tmp_path, json_name="bank_vulnerable")
    res = run_slither(
        target(tmp_path), settings=settings(script, slither_min_impact="informational"), root=ROOT
    )
    assert len(res.findings) == 2 and res.info.min_impact == "informational"


# ---- solc version ---------------------------------------------------------------------

def test_solc_version_is_passed_through_the_environment(tmp_path):
    script, record = make_fake_slither(tmp_path, json_name="empty")
    run_slither(target(tmp_path), settings=settings(script), solc_version="0.8.19")
    assert recorded(record)["solc_version"] == "0.8.19"


def test_solc_version_falls_back_to_the_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("SOLC_VERSION", raising=False)
    script, record = make_fake_slither(tmp_path, json_name="empty")
    run_slither(target(tmp_path), settings=settings(script, solc_version="0.8.34"))
    assert recorded(record)["solc_version"] == "0.8.34"


def test_explicit_solc_version_beats_the_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("SOLC_VERSION", raising=False)
    script, record = make_fake_slither(tmp_path, json_name="empty")
    run_slither(
        target(tmp_path), settings=settings(script, solc_version="0.8.34"), solc_version="0.8.19"
    )
    assert recorded(record)["solc_version"] == "0.8.19"


def test_empty_setting_means_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("SOLC_VERSION", raising=False)
    script, record = make_fake_slither(tmp_path, json_name="empty")
    run_slither(target(tmp_path), settings=settings(script, solc_version=""))
    assert recorded(record)["solc_version"] is None


# ---- the environment the tool runs in -------------------------------------------------

@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="not running inside a virtualenv")
def test_environment_is_pinned_to_the_running_virtualenv(tmp_path, monkeypatch):
    # Whatever shell started us, the tool must see OUR environment: solc-select looks for its
    # state under $VIRTUAL_ENV, so a different value silently means a different state.
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    script, record = make_fake_slither(tmp_path, json_name="empty")
    run_slither(target(tmp_path), settings=settings(script))
    rec = recorded(record)
    assert rec["virtual_env"] == sys.prefix
    assert rec["path"].split(os.pathsep) == [str(Path(sys.prefix) / "bin"), "/usr/bin", "/bin"]


def _fake_venv(tmp_path, monkeypatch, *, with_slither: bool, inside_venv: bool = True) -> Path:
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    if with_slither:
        (bin_dir / "slither").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", "/base" if inside_venv else str(tmp_path / "venv"))
    return bin_dir


def test_a_bare_name_resolves_to_the_virtualenv_copy(tmp_path, monkeypatch):
    bin_dir = _fake_venv(tmp_path, monkeypatch, with_slither=True)
    assert resolve_slither("slither") == str(bin_dir / "slither")


def test_a_bare_name_falls_back_to_the_path_lookup(tmp_path, monkeypatch):
    _fake_venv(tmp_path, monkeypatch, with_slither=False)
    assert resolve_slither("slither") == "slither"


def test_outside_a_virtualenv_nothing_is_guessed(tmp_path, monkeypatch):
    _fake_venv(tmp_path, monkeypatch, with_slither=True, inside_venv=False)
    assert resolve_slither("slither") == "slither"


def test_explicit_paths_are_used_as_given():
    assert resolve_slither("/opt/tools/slither") == "/opt/tools/slither"
    assert resolve_slither("tools/slither") == "tools/slither"


# ---- failures degrade, they do not raise ----------------------------------------------

def test_syntax_error_is_a_compile_failure_with_only_solcs_message(tmp_path):
    script, _ = make_fake_slither(tmp_path, stderr_name="syntax_error", exit_code=1)
    res = run_slither(target(tmp_path), settings=settings(script))
    assert res.info.status == S.COMPILE_FAILED and res.findings == []
    assert res.info.error is not None
    assert res.info.error.startswith("Error: Expected type name")
    assert "Traceback" not in res.info.error and len(res.info.error) < 1600


def test_pragma_vs_solc_mismatch_is_a_compile_failure(tmp_path):
    script, _ = make_fake_slither(tmp_path, stderr_name="version_mismatch", exit_code=1)
    res = run_slither(target(tmp_path), settings=settings(script))
    assert res.info.status == S.COMPILE_FAILED
    assert "requires different compiler version" in (res.info.error or "")


@pytest.mark.parametrize("text", [_UNCONFIGURED_SOLC, _NOT_INSTALLED_SOLC])
def test_an_unusable_solc_is_a_tool_problem_not_a_compile_failure(text):
    status, message = classify_failure(1, "", text)
    assert status == S.TOOL_MISSING
    assert "make setup-solc" in message


def test_a_real_compile_error_is_not_mistaken_for_an_unusable_solc():
    text = (FIX / "syntax_error.stderr.txt").read_text(encoding="utf-8")
    assert classify_failure(1, "", text)[0] == S.COMPILE_FAILED


def test_the_runner_reports_an_unusable_solc_the_same_way(tmp_path):
    script, _ = make_fake_slither(tmp_path, stderr_text=_UNCONFIGURED_SOLC, exit_code=1)
    res = run_slither(target(tmp_path), settings=settings(script))
    assert res.info.status == S.TOOL_MISSING and res.findings == []


def test_unknown_failure_keeps_the_tail_of_stderr(tmp_path):
    script, _ = make_fake_slither(tmp_path, stderr_text="line one\nboom happened\n", exit_code=2)
    res = run_slither(target(tmp_path), settings=settings(script))
    assert res.info.status == S.FAILED
    assert "exited with code 2" in (res.info.error or "") and "boom happened" in (res.info.error or "")


def test_missing_executable(tmp_path):
    res = run_slither(target(tmp_path), settings=settings(tmp_path / "does-not-exist"))
    assert res.info.status == S.TOOL_MISSING and "cannot start" in (res.info.error or "")
    assert res.tool_versions["slither"] == "unknown"


def test_timeout_kills_the_process_and_says_so(tmp_path):
    script, _ = make_fake_slither(tmp_path, sleep_s=30)
    started = time.monotonic()
    res = run_slither(target(tmp_path), settings=settings(script, slither_timeout_s=1.0))
    assert res.info.status == S.TIMEOUT and "1s" in (res.info.error or "")
    assert time.monotonic() - started < 15


def test_json_with_failure_flag(tmp_path):
    script, _ = make_fake_slither(
        tmp_path, json_text='{"success": false, "error": "kaput", "results": {}}'
    )
    res = run_slither(target(tmp_path), settings=settings(script))
    assert res.info.status == S.FAILED and "kaput" in (res.info.error or "")


def test_unreadable_json(tmp_path):
    script, _ = make_fake_slither(tmp_path, json_text="{not json")
    res = run_slither(target(tmp_path), settings=settings(script))
    assert res.info.status == S.FAILED and "unreadable" in (res.info.error or "")


def test_nonzero_exit_is_a_failure_even_if_json_exists(tmp_path):
    script, _ = make_fake_slither(tmp_path, json_name="empty", exit_code=3)
    res = run_slither(target(tmp_path), settings=settings(script))
    assert res.info.status == S.FAILED and "exited with code 3" in (res.info.error or "")


def test_missing_target_is_the_callers_bug_and_raises(tmp_path):
    script, _ = make_fake_slither(tmp_path)
    with pytest.raises(FileNotFoundError):
        run_slither(tmp_path / "nope.sol", settings=settings(script))


# ---- tracing --------------------------------------------------------------------------

def test_success_is_traced_and_the_raw_json_is_kept(tmp_path):
    script, _ = make_fake_slither(tmp_path, json_name="bank_vulnerable")
    tracer = Tracer("r1", tmp_path / "traces")
    res = run_slither(target(tmp_path), settings=settings(script), tracer=tracer, root=ROOT)
    (event,) = [e for e in read_events(tracer.path) if e.kind == "tool"]
    assert (event.name, event.status, event.attrs["status"]) == ("slither", "ok", "ok")
    assert res.info.raw_output_path is not None
    assert (tmp_path / "traces" / res.info.raw_output_path).exists()


def test_failure_marks_the_span_as_error_and_keeps_stderr(tmp_path):
    script, _ = make_fake_slither(tmp_path, stderr_name="syntax_error", exit_code=1)
    tracer = Tracer("r1", tmp_path / "traces")
    res = run_slither(target(tmp_path), settings=settings(script), tracer=tracer)
    (event,) = [e for e in read_events(tracer.path) if e.kind == "tool"]
    assert event.status == "error" and "compile_failed" in (event.error or "")
    assert res.info.raw_output_path is not None
    assert "Traceback" in (tmp_path / "traces" / res.info.raw_output_path).read_text(encoding="utf-8")


# ---- error text extraction ------------------------------------------------------------

def test_extract_compile_error_from_a_real_traceback():
    text = (FIX / "version_mismatch.stderr.txt").read_text(encoding="utf-8")
    out = extract_compile_error(text)
    assert out.startswith("Error: Source file requires different compiler version")
    assert "Traceback" not in out


def test_extract_compile_error_fallbacks():
    assert extract_compile_error("noise\nError: boom\n  --> x.sol:1:1").startswith("Error: boom")
    assert "last line" in extract_compile_error("only\nplain\nlast line")