"""Application settings. Layered: field defaults < .env < environment variables."""
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",          # resolved against cwd; Makefile guarantees cwd = repo root
        env_prefix="AUDIT_",
        extra="ignore",
    )

    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "deepseek-chat"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    max_total_tokens: int = 200_000
    max_cost_usd: float = 0.5
    max_tool_rounds: int = 8

    # Defaults are relative to the repo root (the cwd contract). Override via AUDIT_*_DIR.
    cache_dir: Path = Path("agent/.cache/llm")
    trace_dir: Path = Path("agent/.traces")
    qdrant_dir: Path = Path("agent/.qdrant_data")
    report_dir: Path = Path("reports")


@lru_cache
def get_settings() -> Settings:
    return Settings()