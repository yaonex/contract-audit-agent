from audit_agent.config import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.max_tool_rounds == 8
    assert s.llm_api_key.get_secret_value() == ""


def test_env_override(monkeypatch):
    monkeypatch.setenv("AUDIT_LLM_MODEL", "qwen-plus")
    monkeypatch.setenv("AUDIT_MAX_TOTAL_TOKENS", "1234")
    s = Settings(_env_file=None)
    assert s.llm_model == "qwen-plus"
    assert s.max_total_tokens == 1234


def test_secret_not_leaked_in_repr(monkeypatch):
    monkeypatch.setenv("AUDIT_LLM_API_KEY", "sk-secret")
    s = Settings(_env_file=None)
    assert "sk-secret" not in repr(s)

def test_path_override_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_REPORT_DIR", str(tmp_path / "out"))
    s = Settings(_env_file=None)
    assert s.report_dir == tmp_path / "out"