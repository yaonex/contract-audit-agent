"""Runs the REAL Slither on the hand-written samples.

Needs the project environment (`make sync`) and an installed compiler (`make setup-solc`).
If the compiler is missing, the failure message says so (status tool_missing).
Skipped when Slither itself cannot be found.
"""
import shutil
from pathlib import Path

import pytest
import yaml

from audit_agent.config import get_settings
from audit_agent.schemas.report import StaticAnalysisStatus as S
from audit_agent.static_analysis.slither_runner import resolve_slither, run_slither

REPO = Path(__file__).resolve().parents[3]
EXPECTED = yaml.safe_load((REPO / "samples" / "expected.yaml").read_text(encoding="utf-8"))["samples"]

pytestmark = pytest.mark.skipif(
    shutil.which(resolve_slither(get_settings().slither_path)) is None,
    reason="slither is not installed (run `make sync`)",
)


def analyse(rel: str):
    res = run_slither(REPO / rel, settings=get_settings())
    assert res.info.status == S.OK, res.info.error
    return res


def truth(rel: str) -> list[dict]:
    return next(s for s in EXPECTED if s["file"] == rel)["findings"]


def test_reentrancy_is_found_where_the_ground_truth_says():
    expected = truth("samples/vulnerable/Bank.sol")[0]
    hits = [f for f in analyse("samples/vulnerable/Bank.sol").findings if f.category == "reentrancy"]
    assert len(hits) == 1
    loc = hits[0].location
    assert loc.file == "Bank.sol"
    assert (loc.function, loc.line_start, loc.line_end) == (
        expected["function"], expected["line_start"], expected["line_end"],
    )


def test_the_fixed_bank_has_no_reentrancy_finding():
    assert not [f for f in analyse("samples/safe/Bank.sol").findings if f.category == "reentrancy"]


def test_known_blind_spot_slither_misses_the_missing_owner_check():
    # Documents WHY an LLM is in this project. If Slither ever learns to find this, the
    # assertion fails: update it and the baseline notes on purpose.
    findings = analyse("samples/vulnerable/Vault.sol").findings
    assert not [f for f in findings if f.category == "access_control"]
    assert truth("samples/vulnerable/Vault.sol")[0]["category"] == "access_control"


def test_works_without_uv_run_around_it(monkeypatch):
    # Regression test for "No solc version set". solc-select keeps its state under
    # $VIRTUAL_ENV/.solc-select when VIRTUAL_ENV is set and under ~ otherwise, so the result
    # used to depend on HOW the process was started. The runner must pin this itself: here the
    # shell has no VIRTUAL_ENV and no virtualenv on PATH, as if started as .venv/bin/audit-agent.
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("SOLC_VERSION", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    analyse("samples/vulnerable/Bank.sol")