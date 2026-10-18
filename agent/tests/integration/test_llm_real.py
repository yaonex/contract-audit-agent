import pytest

from audit_agent.config import get_settings
from audit_agent.llm.client import LLMClient
from audit_agent.observability.tracer import Tracer

pytestmark = pytest.mark.skipif(
    not get_settings().llm_api_key.get_secret_value(), reason="no AUDIT_LLM_API_KEY configured"
)


def test_real_call(tmp_path):
    client = LLMClient(get_settings(), tracer=Tracer("it", tmp_path))
    # Reasoning models spend max_tokens on thinking first; keep generous headroom.
    r = client.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=1000)
    assert r.finish_reason == "stop", f"truncated: {r.finish_reason}"
    assert r.content
    assert r.usage.prompt_tokens > 0