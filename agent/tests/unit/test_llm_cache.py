import pytest
import threading

from audit_agent.config import Settings
from audit_agent.llm.types import ChatResult, Usage
from audit_agent.llm.cache import LLMCache, make_cache_key, ReplayMiss

BASE = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.0}


def result(content="hello", finish="stop") -> ChatResult:
    return ChatResult(content=content, finish_reason=finish, usage=Usage(prompt_tokens=3, completion_tokens=2))


def test_key_ignores_dict_order():
    a = {"model": "m", "temperature": 0.0, "messages": []}
    b = {"messages": [], "temperature": 0.0, "model": "m"}
    assert make_cache_key(a, base_url="u") == make_cache_key(b, base_url="u")


def test_key_changes_with_every_request_field():
    base = make_cache_key(BASE, base_url="u")
    variants = [
        {**BASE, "model": "other"},
        {**BASE, "temperature": 0.7},
        {**BASE, "max_tokens": 10},
        {**BASE, "messages": [{"role": "user", "content": "hi!"}]},
        {**BASE, "tools": [{"type": "function"}]},
    ]
    keys = {make_cache_key(v, base_url="u") for v in variants}
    assert base not in keys and len(keys) == len(variants)
    assert make_cache_key(BASE, base_url="other-url") != base  # same model name, other gateway


def test_roundtrip_and_miss(tmp_path):
    c = LLMCache(tmp_path / "c.sqlite")
    assert c.get("nope") is None
    assert c.put("k", "m", result()) is True
    got = c.get("k")
    assert got is not None and got.content == "hello" and got.usage.prompt_tokens == 3


def test_persists_across_instances(tmp_path):
    path = tmp_path / "c.sqlite"
    LLMCache(path).put("k", "m", result("kept"))
    again = LLMCache(path)
    got = again.get("k")
    assert got is not None and got.content == "kept"


def test_truncated_or_unknown_finish_reason_is_not_cached(tmp_path):
    c = LLMCache(tmp_path / "c.sqlite")
    assert c.put("k1", "m", result(content="", finish="length")) is False
    assert c.put("k2", "m", result(finish=None)) is False  # type: ignore[arg-type]
    assert c.get("k1") is None and c.get("k2") is None


def test_tool_calls_finish_reason_is_cached(tmp_path):
    c = LLMCache(tmp_path / "c.sqlite")
    assert c.put("k", "m", result(finish="tool_calls")) is True


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path):
    c = LLMCache(tmp_path / "c.sqlite")
    c._conn.execute(
        "INSERT INTO llm_cache VALUES ('bad', 'm', 'now', 'not-json{')"
    )
    c._conn.commit()
    assert c.get("bad") is None


def test_write_failure_does_not_raise(tmp_path):
    c = LLMCache(tmp_path / "c.sqlite")
    c.close()
    assert c.put("k", "m", result()) is False  # closed DB: logged, not raised
    assert c.get("k") is None


def test_concurrent_puts_are_safe(tmp_path):
    c = LLMCache(tmp_path / "c.sqlite")

    def work(n):
        for i in range(30):
            c.put(f"k{n}-{i}", "m", result())

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert c.get("k5-29") is not None
    assert c._conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 180

def test_from_settings_must_exist_for_replay(tmp_path):
    s = Settings(_env_file=None, cache_dir=tmp_path / "nothing-here")  # type: ignore[call-arg]
    with pytest.raises(FileNotFoundError, match="replay mode"):
        LLMCache.from_settings(s, must_exist=True)
    assert not (tmp_path / "nothing-here").exists()  # and it must not create files


def test_replay_miss_is_not_an_llm_error():
    from audit_agent.llm.client import LLMError
    assert not issubclass(ReplayMiss, LLMError)  # retry loops must not swallow it
