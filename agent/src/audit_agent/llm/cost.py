"""Token / cost accounting and hard budgets for one audit run.

Design notes:
- One CostTracker per run, shared by every LLM call (also across threads: all state is
  behind a lock, so parallel fan-out cannot lose updates).
- Budget is checked BEFORE a call and recorded AFTER it. We cannot know a call's size in
  advance, so a run can overshoot by at most one call (serial) or one per in-flight call
  (parallel). Upgrade to reservation-based accounting only if this proves too loose.
- BudgetExceeded deliberately does NOT subclass LLMError: LLMError means "retry may help",
  while retrying after the budget is gone only burns more money.
- Tokens come from the API's `usage`. For reasoning models this already includes thinking
  tokens, which is why we never estimate from visible output.
- If a provider omits usage, we estimate from character counts and count it in
  `estimated_calls`, so a zero-usage reply can never bypass the budget. That path only has
  LENGTHS, so it assumes ASCII (see estimate_tokens_from_chars); CJK-heavy traffic would be
  under-estimated there. Whitelisted models are verified to return usage, so it is a backstop.
- Cache hits cost nothing and are counted separately, not charged.
- Prices are optional. Without them the USD budget cannot be enforced; we warn loudly.

Size estimation (shared with context/budget.py, written only once):
- No tokenizer is available, so: one ASCII character = 1/3 token, every non-ASCII character =
  1 whole token. Real ratios (my recollection, NOT measured): English prose ~4 chars/token,
  code ~3, CJK 1 or more tokens per character. So the estimate is a little high for prose,
  about right for code, and no longer far too low for CJK. It is a pre-flight size check and a
  safety net; real spend always comes from `usage`.
- The estimate works in integer "units" (1 ASCII char = 1 unit, 3 units = 1 token). Units are
  exactly additive over concatenation, which lets context/budget.py prove a selection fits.
"""
import logging
import threading

from audit_agent.config import Settings
from audit_agent.llm.types import Usage
from audit_agent.schemas.report import CostSummary

logger = logging.getLogger(__name__)

UNITS_PER_TOKEN = 3


def token_units(text: str) -> int:
    """Size of `text` in 1/3-token units: 1 per ASCII character, 3 per non-ASCII character."""
    ascii_chars = len(text.encode("ascii", "ignore"))
    return ascii_chars + UNITS_PER_TOKEN * (len(text) - ascii_chars)


def tokens_from_units(units: int) -> int:
    return -(-max(units, 0) // UNITS_PER_TOKEN)   # ceil


def estimate_tokens(text: str) -> int:
    """Rough token count of `text` (see the module docstring)."""
    return tokens_from_units(token_units(text))


def estimate_tokens_from_chars(chars: int) -> int:
    """Token estimate when only a LENGTH is known; assumes ASCII text."""
    return tokens_from_units(chars)


class BudgetExceeded(Exception):
    """Raised when a hard budget is used up. Not an LLMError: never retry on this."""

    def __init__(self, kind: str, used: float, limit: float) -> None:
        super().__init__(f"{kind} budget exceeded: used {used:g} >= limit {limit:g}")
        self.kind, self.used, self.limit = kind, used, limit


class CostTracker:
    def __init__(
        self,
        *,
        max_total_tokens: int,
        max_cost_usd: float,
        price_in_usd_per_mtok: float | None = None,
        price_out_usd_per_mtok: float | None = None,
    ) -> None:
        self.max_total_tokens = max_total_tokens
        self.max_cost_usd = max_cost_usd
        self._price_in = price_in_usd_per_mtok
        self._price_out = price_out_usd_per_mtok
        self._lock = threading.Lock()
        self._prompt = 0
        self._completion = 0
        self._cost = 0.0
        self._calls = 0
        self._cache_hits = 0
        self._estimated_calls = 0
        if not self.cost_known:
            logger.warning(
                "model prices not configured: USD budget (%.2f) is NOT enforced, "
                "only the token budget is",
                max_cost_usd,
            )

    @classmethod
    def from_settings(cls, s: Settings) -> "CostTracker":
        return cls(
            max_total_tokens=s.max_total_tokens,
            max_cost_usd=s.max_cost_usd,
            price_in_usd_per_mtok=s.llm_price_in_usd_per_mtok,
            price_out_usd_per_mtok=s.llm_price_out_usd_per_mtok,
        )

    @property
    def cost_known(self) -> bool:
        return self._price_in is not None and self._price_out is not None

    def check(self) -> None:
        """Call before every real LLM call. Raises BudgetExceeded if a limit is reached."""
        with self._lock:
            total = self._prompt + self._completion
            if total >= self.max_total_tokens:
                raise BudgetExceeded("token", total, self.max_total_tokens)
            if self.cost_known and self._cost >= self.max_cost_usd:
                raise BudgetExceeded("cost_usd", self._cost, self.max_cost_usd)

    def record(self, usage: Usage, *, prompt_chars: int = 0, completion_chars: int = 0) -> None:
        """Call after every real LLM call. The *_chars arguments are only used if usage is empty."""
        prompt, completion = usage.prompt_tokens, usage.completion_tokens
        estimated = prompt == 0 and completion == 0
        if estimated:
            prompt = estimate_tokens_from_chars(prompt_chars)
            completion = estimate_tokens_from_chars(completion_chars)
        cost = 0.0
        if self.cost_known:
            cost = (prompt * self._price_in + completion * self._price_out) / 1_000_000  # type: ignore[operator]
        with self._lock:
            self._prompt += prompt
            self._completion += completion
            self._cost += cost
            self._calls += 1
            self._estimated_calls += int(estimated)

    def record_cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self._prompt + self._completion

    @property
    def cache_hits(self) -> int:
        with self._lock:
            return self._cache_hits

    @property
    def estimated_calls(self) -> int:
        with self._lock:
            return self._estimated_calls

    def snapshot(self) -> CostSummary:
        """Immutable view for the report. llm_calls counts real calls only."""
        with self._lock:
            return CostSummary(
                prompt_tokens=self._prompt,
                completion_tokens=self._completion,
                cost_usd=round(self._cost, 6),
                llm_calls=self._calls,
            )