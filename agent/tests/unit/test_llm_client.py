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


def make_client(sdk, *, tracer=None, settings=None) -> LLMClient:
    settings = settings or make_settings()
    info = SupportedModel(
        name=settings.llm_model, parallel_tools=False,
        reasoning_content=False, verified_on=date(2026, 1, 1),
    )
    return LLMClient(settings, tracer=tracer, sdk=sdk, model_info=info)


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