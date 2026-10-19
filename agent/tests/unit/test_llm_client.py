import json
from datetime import date
from types import SimpleNamespace as NS

import openai
import pytest
from pydantic import SecretStr

from audit_agent.config import Settings
from audit_agent.llm.client import LLMClient, LLMError
from audit_agent.llm.models import SupportedModel
from audit_agent.observability.tracer import Tracer, read_events
from audit_agent.llm.cost import BudgetExceeded, CostTracker
from audit_agent.llm.cache import LLMCache, ReplayMiss
from audit_agent.llm.types import CacheMode

class Boom(openai.OpenAIError):
    pass


class FakeSDK:
    """Mimics sdk.chat.completions.create(**kwargs)."""

    def __init__(self, response=None, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._response, self._exc = response, exc
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return self._response


def fake_response(content="hi", tool_calls=None, usage=(10, 5), finish="stop"):
    msg = NS(content=content, tool_calls=tool_calls, reasoning_content=None)
    return NS(
        choices=[NS(message=msg, finish_reason=finish)],
        usage=NS(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None,
        model="fake-model",
    )


def make_settings(**over) -> Settings:
    # Fake model name on purpose: tests inject model_info instead of touching the whitelist.
    return Settings(_env_file=None, **{"llm_model": "fake-model", **over})  # type: ignore[call-arg]


def make_client(sdk, *, tracer=None, settings=None, budget=None, cache=None,
                cache_mode=None) -> LLMClient:
    settings = settings or make_settings()
    # Default: no cache object -> OFF, a cache object -> READWRITE (explicit is better in tests).
    if cache_mode is None:
        cache_mode = CacheMode.READWRITE if cache is not None else CacheMode.OFF
    settings = settings.model_copy(update={"cache_mode": cache_mode})
    info = SupportedModel(
        name=settings.llm_model, parallel_tools=False,
        reasoning_content=False, verified_on=date(2026, 1, 1),
    )
    return LLMClient(settings, tracer=tracer, sdk=sdk, model_info=info, budget=budget, cache=cache)


def test_basic_parse():
    sdk = FakeSDK(fake_response())
    r = make_client(sdk).chat([{"role": "user", "content": "x"}])
    assert r.content == "hi"
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (10, 5)
    assert sdk.calls[0]["temperature"] == 0.0


def test_tool_calls_keep_raw_arguments():
    tc = NS(id="c1", function=NS(name="add", arguments='{"a": 1, '))  # truncated JSON on purpose
    r = make_client(FakeSDK(fake_response(content="", tool_calls=[tc]))).chat([])
    assert r.tool_calls[0].name == "add"
    assert r.tool_calls[0].arguments == '{"a": 1, '  # client must not parse or fix it


def test_missing_usage_defaults_to_zero():
    r = make_client(FakeSDK(fake_response(usage=None))).chat([])
    assert r.usage.prompt_tokens == 0


def test_optional_params_only_sent_when_given():
    sdk = FakeSDK(fake_response())
    make_client(sdk).chat([])
    assert "tools" not in sdk.calls[0] and "max_tokens" not in sdk.calls[0]


def test_sdk_errors_are_wrapped():
    with pytest.raises(LLMError, match="Boom"):
        make_client(FakeSDK(exc=Boom("down"))).chat([])


def test_empty_choices_raises():
    with pytest.raises(LLMError):
        make_client(FakeSDK(NS(choices=[], usage=None))).chat([])


def test_unlisted_model_rejected_at_construction():
    # No model_info injected -> production path -> whitelist check.
    with pytest.raises(ValueError, match="not in the verified list"):
        LLMClient(make_settings(llm_model="not-registered"), sdk=FakeSDK(fake_response()))


def test_model_info_must_match_settings():
    info = SupportedModel(
        name="other", parallel_tools=False, reasoning_content=False, verified_on=date(2026, 1, 1)
    )
    with pytest.raises(ValueError, match="!="):
        LLMClient(make_settings(), sdk=FakeSDK(fake_response()), model_info=info)


def test_trace_records_tokens_and_never_leaks_key(tmp_path):
    tracer = Tracer("r1", tmp_path)
    settings = make_settings(llm_api_key=SecretStr("sk-secret"))
    make_client(FakeSDK(fake_response()), tracer=tracer, settings=settings).chat(
        [{"role": "user", "content": "x"}]
    )
    e = read_events(tracer.path)[0]
    assert (e.kind, e.prompt_tokens, e.completion_tokens, e.cache_hit) == ("llm", 10, 5, False)
    blob = (tmp_path / e.attrs["payload_blob"]).read_text(encoding="utf-8")
    assert json.loads(blob)["response"]["content"] == "hi"
    assert "sk-secret" not in tracer.path.read_text(encoding="utf-8") + blob


def test_failed_call_is_traced_as_error(tmp_path):
    tracer = Tracer("r1", tmp_path)
    with pytest.raises(LLMError):
        make_client(FakeSDK(exc=Boom("down")), tracer=tracer).chat([])
    assert read_events(tracer.path)[0].status == "error"

def test_usage_is_recorded_into_budget():
    t = CostTracker(max_total_tokens=1000, max_cost_usd=1.0)
    make_client(FakeSDK(fake_response(usage=(10, 5))), budget=t).chat([])
    assert t.total_tokens == 15


def test_exhausted_budget_blocks_call_before_hitting_the_api():
    t = CostTracker(max_total_tokens=10, max_cost_usd=1.0)
    sdk = FakeSDK(fake_response(usage=(10, 5)))
    client = make_client(sdk, budget=t)
    client.chat([])                       # first call passes and overshoots to 15
    with pytest.raises(BudgetExceeded):
        client.chat([])
    assert len(sdk.calls) == 1            # second call never reached the SDK


def test_failed_call_is_not_charged():
    t = CostTracker(max_total_tokens=1000, max_cost_usd=1.0)
    with pytest.raises(LLMError):
        make_client(FakeSDK(exc=Boom("down")), budget=t).chat([])
    assert t.total_tokens == 0


MSG = [{"role": "user", "content": "x"}]

def test_second_identical_call_is_served_from_cache(tmp_path):
    sdk = FakeSDK(fake_response("hi"))
    budget = CostTracker(max_total_tokens=1000, max_cost_usd=1.0)
    client = make_client(sdk, budget=budget, cache=LLMCache(tmp_path / "c.sqlite"))
    first = client.chat(MSG)
    second = client.chat(MSG)
    assert second.content == first.content
    assert len(sdk.calls) == 1                 # API hit only once
    assert budget.total_tokens == 15           # the hit cost nothing
    assert budget.cache_hits == 1


def test_different_request_is_a_miss(tmp_path):
    sdk = FakeSDK(fake_response())
    client = make_client(sdk, cache=LLMCache(tmp_path / "c.sqlite"))
    client.chat(MSG)
    client.chat([{"role": "user", "content": "y"}])
    assert len(sdk.calls) == 2


def test_cache_hit_still_allowed_when_budget_exhausted(tmp_path):
    cache = LLMCache(tmp_path / "c.sqlite")
    sdk = FakeSDK(fake_response(usage=(10, 5)))
    budget = CostTracker(max_total_tokens=10, max_cost_usd=1.0)
    client = make_client(sdk, budget=budget, cache=cache)
    client.chat(MSG)                            # overshoots the budget
    with pytest.raises(BudgetExceeded):
        client.chat([{"role": "user", "content": "new"}])   # real call: blocked
    assert client.chat(MSG).content == "hi"     # cached call: allowed


def test_truncated_response_is_not_cached(tmp_path):
    sdk = FakeSDK(fake_response(content="", finish="length"))
    client = make_client(sdk, cache=LLMCache(tmp_path / "c.sqlite"))
    client.chat(MSG)
    client.chat(MSG)
    assert len(sdk.calls) == 2                  # second call was NOT served from cache


def test_trace_marks_hit_and_charges_zero_tokens(tmp_path):
    tracer = Tracer("r1", tmp_path)
    client = make_client(FakeSDK(fake_response()), tracer=tracer,
                         cache=LLMCache(tmp_path / "c.sqlite"))
    client.chat(MSG)
    client.chat(MSG)
    miss, hit = read_events(tracer.path)
    assert (miss.cache_hit, miss.prompt_tokens) == (False, 10)
    assert (hit.cache_hit, hit.prompt_tokens, hit.completion_tokens) == (True, 0, 0)
    assert hit.attrs["cached_prompt_tokens"] == 10


def test_off_mode_ignores_an_injected_cache(tmp_path):
    cache = LLMCache(tmp_path / "c.sqlite")
    sdk = FakeSDK(fake_response())
    client = make_client(sdk, cache=cache, cache_mode=CacheMode.OFF)
    client.chat(MSG)
    client.chat(MSG)
    assert len(sdk.calls) == 2                                   # nothing served from cache
    assert cache._conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0  # nothing stored


def _recorded_cache(tmp_path) -> LLMCache:
    """Record one answer for MSG, as an earlier readwrite run would have."""
    cache = LLMCache(tmp_path / "c.sqlite")
    make_client(FakeSDK(fake_response("recorded")), cache=cache).chat(MSG)
    return cache


def test_replay_serves_hits_without_touching_the_api(tmp_path):
    cache = _recorded_cache(tmp_path)
    offline = FakeSDK(exc=Boom("network must not be used"))
    client = make_client(offline, cache=cache, cache_mode=CacheMode.REPLAY)
    assert client.chat(MSG).content == "recorded"
    assert offline.calls == []


def test_replay_miss_raises_and_never_calls_api_or_writes(tmp_path):
    cache = _recorded_cache(tmp_path)
    sdk = FakeSDK(fake_response("should not be used"))
    budget = CostTracker(max_total_tokens=1000, max_cost_usd=1.0)
    client = make_client(sdk, cache=cache, budget=budget, cache_mode=CacheMode.REPLAY)
    with pytest.raises(ReplayMiss):
        client.chat([{"role": "user", "content": "never recorded"}])
    assert sdk.calls == []                                       # API untouched
    assert budget.total_tokens == 0                              # budget untouched
    assert cache._conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 1  # nothing written


def test_replay_needs_no_api_key_and_builds_no_sdk(tmp_path):
    cache = _recorded_cache(tmp_path)
    settings = make_settings(cache_mode=CacheMode.REPLAY)        # api key is empty
    info = SupportedModel(
        name=settings.llm_model, parallel_tools=False,
        reasoning_content=False, verified_on=date(2026, 1, 1),
    )
    client = LLMClient(settings, model_info=info, cache=cache)   # sdk=None on purpose
    assert client._sdk is None
    assert client.chat(MSG).content == "recorded"


def test_trace_records_cache_mode_and_replay_miss_as_error(tmp_path):
    cache = _recorded_cache(tmp_path)
    tracer = Tracer("r1", tmp_path / "t")
    client = make_client(FakeSDK(), tracer=tracer, cache=cache, cache_mode=CacheMode.REPLAY)
    with pytest.raises(ReplayMiss):
        client.chat([{"role": "user", "content": "never recorded"}])
    e = read_events(tracer.path)[0]
    assert e.status == "error" and e.attrs["cache_mode"] == "replay"