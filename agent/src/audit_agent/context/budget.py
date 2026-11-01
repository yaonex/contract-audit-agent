"""Fitting numbered source lines into a token budget.

Rules:
- Everything is measured with the shared estimate from llm/cost.py, in integer units that add
  up exactly, so "the result fits" is a proven property (see the tests), not a hope.
- If everything fits, everything is kept. Otherwise keep the START and the END of the file,
  half of the budget each, and put an explicit marker where lines were dropped. The start of
  a file holds pragma / imports / state variables, the end usually the main contract's
  functions. That is a guess, not data: if truncation turns out to be frequent, that is the
  signal to build function-level slicing (Phase 1.4).
- Lines keep their ORIGINAL numbers (callers number them before selecting), so a line after
  the marker is still called by the number Slither and the IDE use.
- MIN_CONTEXT_TOKENS exists so that, with the per-line cap of source.py, a head and a tail
  of at least one line each always fit. Below it we refuse instead of producing a context
  that silently shows almost nothing.
"""
from dataclasses import dataclass

from audit_agent.llm.cost import UNITS_PER_TOKEN, estimate_tokens, token_units

MIN_CONTEXT_TOKENS = 8_000    # keep equal to the floor of Settings.context_max_tokens (tested)
_NEWLINE_UNITS = 1


@dataclass(frozen=True)
class Selection:
    included: tuple[tuple[int, int], ...]   # 1-based inclusive line ranges that were kept
    text: str
    est_tokens: int


def omitted_marker(first: int, last: int) -> str:
    count = last - first + 1
    where = f"line {first}" if count == 1 else f"lines {first}-{last}"
    plural = "" if count == 1 else "s"
    return f"[... {where} omitted ({count} line{plural}) ...]"


def select_lines(rendered: list[str], max_tokens: int) -> Selection:
    """Pick the lines to show. `rendered[i]` is the numbered text of line i + 1."""
    if max_tokens < MIN_CONTEXT_TOKENS:
        raise ValueError(
            f"context budget {max_tokens} is below the minimum of {MIN_CONTEXT_TOKENS} tokens"
        )
    if not rendered:
        raise ValueError("nothing to select from")

    total = len(rendered)
    costs = [token_units(line) + _NEWLINE_UNITS for line in rendered]   # +1: the joining "\n"
    budget_units = max_tokens * UNITS_PER_TOKEN

    if sum(costs) <= budget_units:
        text = "\n".join(rendered)
        return Selection(((1, total),), text, estimate_tokens(text))

    # The marker is the same size or smaller than this worst case (largest numbers).
    reserve = token_units(f"[... lines {total}-{total} omitted ({total} lines) ...]") + _NEWLINE_UNITS
    available = budget_units - reserve
    head_budget = available // 2
    tail_budget = available - head_budget

    head, used = 0, 0
    while head < total and used + costs[head] <= head_budget:
        used += costs[head]
        head += 1
    tail, used = 0, 0
    while tail < total - head and used + costs[total - 1 - tail] <= tail_budget:
        used += costs[total - 1 - tail]
        tail += 1

    if head == 0 and tail == 0:
        raise ValueError("the budget is too small to hold even a single line")
    if head + tail >= total:   # impossible: the whole file did not fit above
        raise RuntimeError("budget invariant broken")

    ranges: list[tuple[int, int]] = []
    if head:
        ranges.append((1, head))
    if tail:
        ranges.append((total - tail + 1, total))
    parts = [*rendered[:head], omitted_marker(head + 1, total - tail), *rendered[total - tail:]]
    text = "\n".join(parts)
    return Selection(tuple(ranges), text, estimate_tokens(text))