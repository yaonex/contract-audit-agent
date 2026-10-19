import threading

import pytest

from audit_agent.llm.client import Usage
from audit_agent.llm.cost import BudgetExceeded, CostTracker


def tracker(**over) -> CostTracker:
    base = dict(max_total_tokens=1000, max_cost_usd=1.0)
    return CostTracker(**{**base, **over})


def test_accumulates_tokens():
    t = tracker()
    t.record(Usage(prompt_tokens=100, completion_tokens=50))
    t.record(Usage(prompt_tokens=10, completion_tokens=5))
    s = t.snapshot()
    assert (s.prompt_tokens, s.completion_tokens, s.llm_calls) == (110, 55, 2)
    assert s.total_tokens == 165


def test_token_budget_blocks_next_call_not_the_current_one():
    t = tracker(max_total_tokens=100)
    t.check()  # fresh: fine
    t.record(Usage(prompt_tokens=90, completion_tokens=20))  # overshoots to 110
    with pytest.raises(BudgetExceeded) as exc:
        t.check()
    assert exc.value.kind == "token"


def test_cost_computed_and_enforced_when_prices_known():
    t = tracker(max_total_tokens=10**9, max_cost_usd=0.01, price_in_usd_per_mtok=2.0, price_out_usd_per_mtok=8.0)
    t.record(Usage(prompt_tokens=1_000_000, completion_tokens=0))  # $2.00
    assert t.snapshot().cost_usd == pytest.approx(2.0)
    with pytest.raises(BudgetExceeded) as exc:
        t.check()
    assert exc.value.kind == "cost_usd"


def test_without_prices_cost_is_zero_and_usd_budget_not_enforced(caplog):
    with caplog.at_level("WARNING"):
        t = tracker(max_cost_usd=0.0)
    assert "NOT enforced" in caplog.text
    t.record(Usage(prompt_tokens=10, completion_tokens=10))
    t.check()  # would raise if the USD budget were (wrongly) applied
    assert t.snapshot().cost_usd == 0.0


def test_missing_usage_is_estimated_high_and_flagged():
    t = tracker()
    t.record(Usage(), prompt_chars=300, completion_chars=31)
    s = t.snapshot()
    assert s.prompt_tokens == 100 and s.completion_tokens == 11  # ceil(31/3)
    assert t.estimated_calls == 1


def test_estimate_cannot_bypass_budget():
    t = tracker(max_total_tokens=50)
    t.record(Usage(), prompt_chars=3000)  # ~1000 tokens estimated
    with pytest.raises(BudgetExceeded):
        t.check()


def test_cache_hits_are_free_and_counted_separately():
    t = tracker()
    t.record_cache_hit()
    assert t.cache_hits == 1
    assert t.snapshot().llm_calls == 0 and t.total_tokens == 0


def test_budget_exceeded_is_not_an_llm_error():
    from audit_agent.llm.client import LLMError
    assert not issubclass(BudgetExceeded, LLMError)  # retry loops must not swallow it


def test_concurrent_recording_loses_nothing():
    t = tracker(max_total_tokens=10**9)

    def work():
        for _ in range(500):
            t.record(Usage(prompt_tokens=1, completion_tokens=1))

    threads = [threading.Thread(target=work) for _ in range(8)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert t.snapshot().llm_calls == 4000
    assert t.total_tokens == 8000