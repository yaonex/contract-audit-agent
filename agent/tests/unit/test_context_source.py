import json
from pathlib import Path

import pytest

from audit_agent.context.budget import MIN_CONTEXT_TOKENS, omitted_marker
from audit_agent.context.source import (
    MAX_FILE_BYTES,
    RangeStatus,
    build_context,
    build_context_from_text,
    find_imports,
    split_lines,
)
from audit_agent.llm.cost import estimate_tokens

REPO = Path(__file__).resolve().parents[3]
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "slither-0.11.6"

BIG = "\n".join(f"    uint256 value{i} = {i};" for i in range(1, 3001))


def ctx_of(text: str, **kw):
    return build_context_from_text(text, file="A.sol", **kw)


def rows(ctx) -> list[str]:
    # NOT splitlines(): see the module docstring of source.py
    return ctx.text.split("\n")


# ---- how lines are counted ------------------------------------------------------------

@pytest.mark.parametrize(("text", "expected"), [
    ("a\nb\n", ["a", "b"]),
    ("a\nb", ["a", "b"]),
    ("a\r\nb\r\n", ["a", "b"]),
    ("a\n\nb", ["a", "", "b"]),
    ("a\n\n", ["a", ""]),
    ("", []),
])
def test_split_lines(text, expected):
    assert split_lines(text) == expected


def test_only_a_newline_splits_lines():
    # form feed, NEL and LINE SEPARATOR break str.splitlines() but not a compiler's count
    assert split_lines("a\x0cb\u2028c\x85d\ne") == ["a\x0cb\u2028c\x85d", "e"]


def test_a_final_newline_does_not_add_a_line():
    with_nl, without_nl = ctx_of("a\nb\n"), ctx_of("a\nb")
    assert with_nl.total_lines == without_nl.total_lines == 2
    assert with_nl.text == without_nl.text


def test_windows_line_endings_leave_no_trace(tmp_path):
    p = tmp_path / "A.sol"
    p.write_bytes(b"a\r\nb\r\n")
    ctx = build_context(p)
    assert ctx.text == "1 | a\n2 | b" and "\r" not in ctx.text


def test_a_byte_order_mark_is_dropped(tmp_path):
    p = tmp_path / "A.sol"
    p.write_bytes(b"\xef\xbb\xbfpragma solidity 0.8.34;\ncontract A {}\n")
    ctx = build_context(p)
    assert rows(ctx)[0] == "1 | pragma solidity 0.8.34;" and ctx.total_lines == 2


# ---- numbering ------------------------------------------------------------------------

def test_every_line_is_numbered_including_blank_ones():
    ctx = ctx_of("a\n\nb\n")
    assert ctx.text == "1 | a\n2 |\n3 | b"
    assert ctx.included_ranges == ((1, 3),) and not ctx.truncated


def test_width_follows_the_line_count():
    ctx = ctx_of("\n".join(f"l{i}" for i in range(1, 11)))
    assert (rows(ctx)[0], rows(ctx)[9]) == (" 1 | l1", "10 | l10")


def test_indentation_is_kept_and_only_empty_lines_lose_the_trailing_space():
    ctx = ctx_of("    x\n\n   \ny")
    assert rows(ctx) == ["1 |     x", "2 |", "3 | " + "   ", "4 | y"]


def test_a_very_long_line_is_shortened_visibly():
    ctx = ctx_of("x" * 5000 + "\ny")
    assert rows(ctx) == ["1 | " + "x" * 2000 + " [... 3000 more chars]", "2 | y"]
    assert ctx.n_long_lines == 1


def test_output_is_deterministic_and_has_no_absolute_paths(tmp_path):
    p = tmp_path / "sub" / "Bank.sol"
    p.parent.mkdir()
    p.write_text("contract A {}\n", encoding="utf-8")
    first, second = build_context(p, root=tmp_path), build_context(p, root=tmp_path)
    assert first == second
    assert first.file == "sub/Bank.sol"
    assert str(tmp_path) not in first.text


def test_a_file_outside_the_root_is_reported_by_name_only(tmp_path):
    p = tmp_path / "Bank.sol"
    p.write_text("contract A {}\n", encoding="utf-8")
    assert build_context(p, root=tmp_path / "elsewhere").file == "Bank.sol"
    assert build_context(p).file == "Bank.sol"


# ---- the hand-written samples ---------------------------------------------------------

def sample_rows(rel: str):
    ctx = build_context(REPO / rel)
    return ctx, rows(ctx)


def test_vulnerable_bank_numbering():
    ctx, r = sample_rows("samples/vulnerable/Bank.sol")
    assert (ctx.total_lines, ctx.file, ctx.truncated, ctx.imports) == (18, "Bank.sol", False, ())
    assert r[0] == " 1 | // SPDX-License-Identifier: MIT"
    assert r[2] == " 3 |"
    assert r[10] == "11 |     function withdraw() external {"
    assert r[13] == '14 |         (bool ok, ) = msg.sender.call{value: amount}("");'
    assert r[15] == "16 |         balances[msg.sender] = 0;"
    assert r[17] == "18 | }"


def test_safe_bank_numbering():
    ctx, r = sample_rows("samples/safe/Bank.sol")
    assert ctx.total_lines == 18
    assert r[13] == "14 |         balances[msg.sender] = 0;"
    assert r[14] == '15 |         (bool ok, ) = msg.sender.call{value: amount}("");'


def test_vault_numbering():
    ctx, r = sample_rows("samples/vulnerable/Vault.sol")
    assert ctx.total_lines == 21
    assert r[10] == "11 |     function deposit() external payable {}"
    assert r[12] == "13 |     function setOwner(address newOwner) external {"
    assert r[20] == "21 | }"


# ---- reconciliation with REAL Slither output ------------------------------------------

def _detector(fixture: str, check: str) -> dict:
    data = json.loads((FIX / f"{fixture}.json").read_text(encoding="utf-8"))
    return next(d for d in data["results"]["detectors"] if d["check"] == check)


def test_slithers_function_range_matches_our_numbering():
    ctx = build_context(REPO / "samples/vulnerable/Bank.sol")
    elements = _detector("bank_vulnerable", "reentrancy-eth")["elements"]
    function = next(e for e in elements if e["type"] == "function")
    first, last = function["source_mapping"]["lines"][0], function["source_mapping"]["lines"][-1]
    assert "function withdraw" in rows(ctx)[first - 1]
    assert rows(ctx)[last - 1].strip().endswith("}")
    call = next(e for e in elements if e["type"] == "node" and "call" in e["name"])
    assert "msg.sender.call" in rows(ctx)[call["source_mapping"]["lines"][0] - 1]


def test_slithers_contract_range_overshoots_a_file_without_final_newline():
    ctx = build_context(REPO / "samples/vulnerable/Vault.sol")
    element = _detector("vault", "missing-zero-check")["elements"][0]
    contract = element["type_specific_fields"]["parent"]["type_specific_fields"]["parent"]
    lines = contract["source_mapping"]["lines"]
    assert (min(lines), max(lines)) == (4, 22) and ctx.total_lines == 21     # observed: one too many
    assert ctx.range_status(4, 22) == RangeStatus.OUTSIDE_FILE
    assert ctx.clamp(4, 22) == (4, 21)


# ---- truncation and what the model saw ------------------------------------------------

def big():
    return ctx_of(BIG, max_tokens=MIN_CONTEXT_TOKENS)


def test_truncation_is_explicit_and_keeps_the_original_numbers():
    ctx = big()
    assert ctx.truncated and ctx.total_lines == 3000
    (_, head_end), (tail_start, tail_end) = ctx.included_ranges
    assert tail_end == 3000
    assert ctx.omitted_ranges == ((head_end + 1, tail_start - 1),)
    r = rows(ctx)
    assert r[0].startswith("   1 |")
    assert r[head_end] == omitted_marker(head_end + 1, tail_start - 1)
    assert r[head_end + 1].startswith(f"{tail_start:>4} |")
    assert ctx.est_tokens == estimate_tokens(ctx.text) <= ctx.max_tokens


def test_range_status_distinguishes_shown_hidden_and_invalid():
    ctx = big()
    hidden_start, hidden_end = ctx.omitted_ranges[0]
    assert ctx.range_status(1, 1) == RangeStatus.OK
    assert ctx.range_status(3000, 3000) == RangeStatus.OK
    assert ctx.range_status(hidden_start - 1, hidden_start - 1) == RangeStatus.OK
    assert ctx.range_status(hidden_end + 1, hidden_end + 1) == RangeStatus.OK
    assert ctx.range_status(hidden_start, hidden_start) == RangeStatus.IN_OMITTED
    assert ctx.range_status(hidden_start - 3, hidden_start) == RangeStatus.IN_OMITTED   # partly hidden
    assert ctx.range_status(0, 3) == RangeStatus.OUTSIDE_FILE
    assert ctx.range_status(2999, 3001) == RangeStatus.OUTSIDE_FILE
    assert ctx.range_status(5, 4) == RangeStatus.OUTSIDE_FILE


def test_clamp():
    ctx = ctx_of("\n".join("x" for _ in range(18)))
    assert ctx.clamp(4, 19) == (4, 18)
    assert ctx.clamp(1, 18) == (1, 18)
    assert ctx.clamp(0, 3) == (1, 3)
    assert ctx.clamp(19, 25) is None            # invented, not repaired
    assert ctx.clamp(-5, 0) is None
    with pytest.raises(ValueError):
        ctx.clamp(5, 4)


def test_info_mirrors_the_context():
    info = big().to_info()
    ctx = big()
    assert (info.file, info.total_lines, info.truncated) == ("A.sol", 3000, True)
    assert info.included_ranges == list(ctx.included_ranges)
    assert (info.est_tokens, info.max_tokens) == (ctx.est_tokens, ctx.max_tokens)


# ---- imports --------------------------------------------------------------------------

@pytest.mark.parametrize(("code", "expected"), [
    ('import "./A.sol";', ['import "./A.sol";']),
    ('import {A, B} from "./A.sol";', ['import {A, B} from "./A.sol";']),
    ('import * as X from "x.sol";', ['import * as X from "x.sol";']),
    ('import {\n    A,\n    B\n} from "./A.sol";', ['import { A, B } from "./A.sol";']),
    ('pragma solidity 0.8.34; import "./A.sol";', ['import "./A.sol";']),
    ('import "./A.sol";\nimport "./A.sol";', ['import "./A.sol";']),
    ('// import "x.sol";\n/* import "y.sol";\n   import "z.sol"; */\ncontract A {}', []),
    ("important = 1;", []),
    ("contract A {}", []),
])
def test_find_imports(code, expected):
    assert list(find_imports(code)) == expected


def test_imports_are_reported_not_followed():
    ctx = ctx_of('import "./Other.sol";\ncontract A {}\n')
    assert ctx.imports == ('import "./Other.sol";',) and ctx.total_lines == 2


# ---- failures -------------------------------------------------------------------------

def test_a_missing_file_is_the_callers_mistake(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_context(tmp_path / "nope.sol")


def test_a_directory_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="single files"):
        build_context(tmp_path)


@pytest.mark.parametrize("content", [b"", b"\n\n  \t\n"])
def test_empty_sources_are_rejected(tmp_path, content):
    p = tmp_path / "A.sol"
    p.write_bytes(content)
    with pytest.raises(ValueError, match="empty"):
        build_context(p)


def test_an_oversized_file_is_rejected_unread(tmp_path):
    p = tmp_path / "Big.sol"
    p.write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="refusing"):
        build_context(p)


def test_invalid_utf8_is_replaced_and_flagged_without_moving_line_numbers(tmp_path):
    p = tmp_path / "A.sol"
    p.write_bytes(b"// caf\xe9 \xff\ncontract A {}\n")
    ctx = build_context(p)
    assert ctx.had_decode_errors and ctx.total_lines == 2
    assert "\ufffd" in rows(ctx)[0] and rows(ctx)[1] == "2 | contract A {}"
    assert not ctx_of("contract A {}").had_decode_errors


def test_a_budget_below_the_minimum_is_rejected():
    with pytest.raises(ValueError, match="minimum"):
        ctx_of("contract A {}", max_tokens=100)