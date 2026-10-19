"""Application settings. Layered: field defaults < .env < environment variables.

Design notes:
- Paths are relative to the repo root. The Makefile guarantees cwd = repo root, so the code
  never hard-codes locations (same idea as Viper layered config in Go).
- Model *connection* info (URL / key / model name) lives here because it is a deployment
  choice and changes often. Model *capabilities* do NOT live here: they are verified facts
  about a specific model, kept in llm/models.py (whitelist).
"""
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",          # resolved against cwd; Makefile guarantees cwd = repo root
        env_prefix="AUDIT_",      # avoids clashing with OPENAI_API_KEY etc. in the environment
        extra="ignore",
    )

    # --- LLM (OpenAI-compatible endpoint) ---
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr = SecretStr("")   # SecretStr: never printed or logged by accident
    llm_model: str = "deepseek-flash"         # must be listed in llm/models.py SUPPORTED_MODELS
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2                 # transport retries, handled by the openai SDK

    # --- Pricing (USD per 1M tokens), from the provider's pricing page ---
    # None = unknown: token budget still enforced, USD budget cannot be. Convert if the page
    # quotes another currency. Prices change, so they live here, not in the model whitelist.
    llm_price_in_usd_per_mtok: float = 0.2
    llm_price_out_usd_per_mtok: float = 0.4

    # --- Budgets (hard limits per audit run, enforced in llm/cost.py) ---
    max_total_tokens: int = 200_000
    max_cost_usd: float = 0.5
    max_tool_rounds: int = 8

    # --- Paths (aligned with .gitignore; override via AUDIT_*_DIR / AUDIT_TAXONOMY_PATH) ---
    cache_dir: Path = Path("agent/.cache/llm")
    trace_dir: Path = Path("agent/.traces")
    qdrant_dir: Path = Path("agent/.qdrant_data")
    report_dir: Path = Path("reports")
    taxonomy_path: Path = Path("knowledge/taxonomy.yaml")


@lru_cache
def get_settings() -> Settings:
    # Cached singleton. Tests must build Settings(...) directly, not call this.
    return Settings()