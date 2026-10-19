"""Persistent LLM response cache (SQLite, stdlib only).

Purpose: (1) save money/time, (2) pin results so experiments are comparable. temperature=0
does not guarantee identical outputs and aliased models can change silently; a cached
response is the only thing that pins an output. Caveat: a cache HIDES model randomness, it
does not remove it. Experiments that change the LLM input (e.g. dropping RAG) change the key
and miss the cache, so they still need repeated real samples (CacheMode.OFF) with variance
reported. When to use which mode: see CacheMode in types.py.

Design notes:
- Key = SHA-256 of the canonical JSON of the FULL request (model, messages, temperature,
  tools, max_tokens, response_format) + base_url + a schema version. Any change to the
  prompt text changes the key, so prompt versioning needs no extra mechanism.
- Python's built-in hash() is randomized per process, so it can never be a persistent key.
- Only complete answers are stored. A response cut off by max_tokens (finish_reason=length)
  would otherwise be replayed forever.
- The cache must never break a run: read errors count as a miss, write errors are logged.
- ReplayMiss (replay mode, key not found) is NOT an LLMError: retrying cannot help.
- Known limits, accepted for the MVP: two threads missing on the same key at once both call
  the API (last write wins); no per-sample id yet (planned for Phase 1.5 multi-sampling).
- To invalidate everything, delete the database file (or bump CACHE_SCHEMA_VERSION).
"""
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from audit_agent.config import Settings
from audit_agent.llm.types import ChatResult

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1
# Whitelist, not blacklist: unknown finish reasons are not cached.
CACHEABLE_FINISH_REASONS = frozenset({"stop", "tool_calls"})


class ReplayMiss(Exception):
    """Replay mode: the request is not in the cache. Never retry on this."""

    def __init__(self, key: str) -> None:
        super().__init__(
            f"replay miss (key {key[:12]}): request not found in cache. "
            "The prompt/context/params changed since recording; re-record with "
            "AUDIT_CACHE_MODE=readwrite."
        )
        self.key = key


def make_cache_key(request: dict[str, Any], *, base_url: str) -> str:
    canonical = json.dumps(
        {"v": CACHE_SCHEMA_VERSION, "base_url": base_url, "request": request},
        sort_keys=True,          # dict order must not change the key
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # One shared connection guarded by a lock; threads may call get/put concurrently.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS llm_cache ("
                " key TEXT PRIMARY KEY, model TEXT NOT NULL,"
                " created_at TEXT NOT NULL, response TEXT NOT NULL)"
            )
            self._conn.commit()

    @classmethod
    def from_settings(cls, s: Settings, *, must_exist: bool = False) -> "LLMCache":
        path = s.cache_dir / "llm_cache.sqlite"
        # Replay needs recorded answers. Without this check sqlite would silently create an
        # empty database and every request would "miss" with a misleading message.
        if must_exist and not path.exists():
            raise FileNotFoundError(f"replay mode needs an existing cache file: {path}")
        return cls(path)

    def get(self, key: str) -> ChatResult | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT response FROM llm_cache WHERE key = ?", (key,)
                ).fetchone()
        except sqlite3.Error:
            logger.warning("llm cache read failed; treating as miss", exc_info=True)
            return None
        if row is None:
            return None
        try:
            return ChatResult.model_validate_json(row[0])
        except ValidationError:
            logger.warning("corrupt llm cache entry %s; treating as miss", key[:12])
            return None

    def put(self, key: str, model: str, result: ChatResult) -> bool:
        """Store a result. Returns False if it was skipped or failed (never raises)."""
        if result.finish_reason not in CACHEABLE_FINISH_REASONS:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, model, created_at, response)"
                    " VALUES (?, ?, ?, ?)",
                    (key, model, datetime.now(UTC).isoformat(), result.model_dump_json()),
                )
                self._conn.commit()
            return True
        except sqlite3.Error:
            logger.warning("llm cache write failed", exc_info=True)
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()