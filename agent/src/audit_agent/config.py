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
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from audit_agent.llm.types import CacheMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",          # resolved against cwd; Makefile guarantees cwd = repo root
        env_prefix="AUDIT_",      # avoids clashing with OPENAI_API_KEY etc. in the environment
        extra="ignore",
    )

    # --- LLM (OpenAI-compatible endpoint) ---
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr = SecretStr("")   # SecretStr: never printed or logged by accident
    llm_model: str = "deepseek-flash"        # must be listed in llm/models.py SUPPORTED_MODELS
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2                 # transport retries, handled by the openai SDK

    # --- Response cache (llm/cache.py) ---
    # Default OFF: a cache must be switched on explicitly (eval scripts do). See CacheMode.
    cache_mode: CacheMode = CacheMode.OFF

    # --- Budgets (hard limits per audit run, enforced in llm/cost.py) ---
    max_total_tokens: int = 200_000
    max_cost_usd: float = 0.5
    max_tool_rounds: int = 8
    agent_timeout_s: float = 300.0   # total wall-clock limit of one agent run; only checked BETWEEN calls

    # --- Pricing (USD per 1M tokens), from the provider's pricing page ---
    # None = unknown: token budget still enforced, USD budget cannot be. Convert if the page
    # quotes another currency. Prices change, so they live here, not in the model whitelist.
    llm_price_in_usd_per_mtok: float | None = None
    llm_price_out_usd_per_mtok: float | None = None

    # --- Source context (context/source.py) ---
    # Token budget for the contract text shown to the model (an ESTIMATE, see llm/cost.py).
    # The floor equals context.budget.MIN_CONTEXT_TOKENS: below it, one long line could leave
    # no room for any line at all. A test keeps the two numbers equal.
    context_max_tokens: int = Field(default=20_000, ge=8_000)

    # --- Static analysis (static_analysis/slither_runner.py) ---
    # Slither is a PROJECT dependency (pyproject, pinned). A bare name is looked up in the
    # project's virtualenv first, then on PATH. A value with a "/" is used as given.
    slither_path: str = "slither"
    slither_timeout_s: float = 120.0    # a guess: Slither needed ~1 s on the samples
    # Findings below this impact are dropped when parsing (the raw JSON keeps everything).
    # "optimization" results are never kept: they are not security findings.
    slither_min_impact: Literal["informational", "low", "medium", "high"] = "low"
    # Pins the solc version for every run (passed as SOLC_VERSION) and is what
    # `make setup-solc` installs. Without it solc-select's global selection applies: hidden
    # state. Empty string means "not set".
    solc_version: str | None = None

    # --- Report (report/) ---
    report_max_snippet_lines: int = 12
    report_max_snippets_per_finding: int = 3

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