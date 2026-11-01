import pytest

from audit_agent.context.budget import MIN_CONTEXT_TOKENS, omitted_marker, select_lines
from audit_agent.llm.cost import estimate_tokens


def numbered(n: int, body: str = "x" * 60) -> list[str]:
    return [f"{i:>4} | {body}" for i in range(1, n + 1)]


def test_everything_that_fits_is_kept_untouched():
    rendered = numbered(50)
    sel = select_lines(rendered, 20_000)
    assert sel.included == ((1, 50),)
    assert sel.text == "\n".join(rendered)
    assert sel.est_tokens == estimate_tokens(sel.text)


@pytest.mark.parametrize(("n", "body", "max_tokens"), [
    (2000, "x" * 60, 20_000),
    (5000, "x" * 10, MIN_CONTEXT_TOKENS),
    (400, "x" * 1500, MIN_CONTEXT_TOKENS),
    (3000, "合约" * 20, 20_000),          # non-ASCII text costs more
])
def test_the_result_always_fits_the_budget(n, body, max_tokens):
    sel = select_lines(numbered(n, body), max_tokens)
    assert estimate_tokens(sel.text) <= max_tokens
    assert sel.est_tokens == estimate_tokens(sel.text)


def test_too_big_keeps_head_and_tail_with_a_marker_in_between():
    rendered = numbered(2000)
    sel = select_lines(rendered, 20_000)
    (head_start, head_end), (tail_start, tail_end) = sel.included
    assert (head_start, tail_end) == (1, 2000)
    assert head_end + 1 <= tail_start - 1                       # at least one line is left out
    lines = sel.text.split("\n")
    assert lines[head_end] == omitted_marker(head_end + 1, tail_start - 1)
    assert lines[head_end + 1] == rendered[tail_start - 1]      # original numbering continues
    assert len(lines) == head_end + 1 + (2000 - tail_start + 1)


def test_head_and_tail_get_about_half_each():
    (_, head_end), (tail_start, _) = select_lines(numbered(2000), 20_000).included
    assert abs(head_end - (2000 - tail_start + 1)) <= 1


def test_non_ascii_lines_are_more_expensive():
    ascii_kept = sum(e - s + 1 for s, e in select_lines(numbered(3000, "x" * 40), 20_000).included)
    cjk_kept = sum(e - s + 1 for s, e in select_lines(numbered(3000, "码" * 40), 20_000).included)
    assert cjk_kept < ascii_kept


def test_markers_read_naturally():
    assert omitted_marker(5, 5) == "[... line 5 omitted (1 line) ...]"
    assert omitted_marker(200, 639) == "[... lines 200-639 omitted (440 lines) ...]"


def test_below_the_minimum_budget_is_refused():
    with pytest.raises(ValueError, match="minimum"):
        select_lines(numbered(5), MIN_CONTEXT_TOKENS - 1)


def test_nothing_to_select_from():
    with pytest.raises(ValueError):
        select_lines([], 20_000)


def test_lines_too_big_for_the_budget_are_refused_not_silently_dropped():
    with pytest.raises(ValueError, match="single line"):
        select_lines(["x" * 30_000] * 3, MIN_CONTEXT_TOKENS)