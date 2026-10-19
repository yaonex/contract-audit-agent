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
- If a provider omits usage, we estimate from character counts (deliberately high) and
  count it in `estimated_calls`, so a zero-usage reply can never bypass the budget.
- Cache hits cost nothing and are counted separately, not charged.
- Prices are optional. Without them the USD budget cannot be enforced; we warn loudly.
"""
import logging
import threading

from audit_agent.config import Settings
from audit_agent.schemas.report import CostSummary

logger = logging.getLogger(__name__)

# Chars-per-token used only when the provider returns no usage. Real text is ~4 chars/token
# in English and fewer for code/CJK, so 3 over-estimates on purpose (budget is a safety net).
_CHARS_PER_TOKEN_ESTIMATE = 3


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

    def record(self, usage, *, prompt_chars: int = 0, completion_chars: int = 0) -> None:
        """Call after every real LLM call. `usage` is a client.Usage (duck-typed here to
        avoid a circular import). The *_chars arguments are only used if usage is empty."""
        prompt, completion = usage.prompt_tokens, usage.completion_tokens
        estimated = prompt == 0 and completion == 0
        if estimated:
            prompt = -(-prompt_chars // _CHARS_PER_TOKEN_ESTIMATE)          # ceil
            completion = -(-completion_chars // _CHARS_PER_TOKEN_ESTIMATE)  # ceil
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