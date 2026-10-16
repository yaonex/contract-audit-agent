"""JSONL tracer: one line per span (LLM call, tool call, graph node, ...).

Design notes:
- Append-only JSONL: crash-tolerant, greppable.
- Parent/child span links via contextvars (no manual plumbing).
- Large payloads (full prompts, forge logs) go to blob files; events store paths only.
- Tracing must never break the audit: write failures are logged, not raised.
"""
import itertools
import json
import logging
import re
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_current_span: ContextVar[str | None] = ContextVar("current_span", default=None)

# Fields promoted to typed columns because we aggregate over them (cost, cache stats).
_TYPED_FIELDS = {"model", "prompt_tokens", "completion_tokens", "cache_hit"}
_MAX_ERROR_CHARS = 500


class TraceEvent(BaseModel):
    ts: datetime
    run_id: str
    span_id: str
    parent_id: str | None = None
    kind: str                       # "llm" | "tool" | "node" | ...
    name: str
    status: Literal["ok", "error"] = "ok"
    duration_ms: float
    error: str | None = None
    # Typed fields: frequently aggregated.
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_hit: bool | None = None
    # Everything else (tool name, args summary, blob paths, ...).
    attrs: dict[str, Any] = Field(default_factory=dict)


class SpanHandle:
    """Lets the code inside a `with` block attach data discovered during the call."""

    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}
        self.attrs: dict[str, Any] = {}

    def set(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            (self.fields if key in _TYPED_FIELDS else self.attrs)[key] = value


class Tracer:
    def __init__(self, run_id: str, base_dir: Path) -> None:
        self.run_id = run_id
        self.base_dir = Path(base_dir)
        run_dir = self.base_dir / run_id
        (run_dir / "blobs").mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "trace.jsonl"
        self._lock = threading.Lock()
        self._blob_seq = itertools.count(1)

    @contextmanager
    def span(self, kind: str, name: str, **attrs: Any) -> Generator[SpanHandle]:
        span_id = uuid4().hex[:12]
        parent_id = _current_span.get()
        token = _current_span.set(span_id)
        handle = SpanHandle()
        handle.set(**attrs)
        start = time.perf_counter()
        error: str | None = None
        try:
            yield handle
        except BaseException as exc:  # record, then always re-raise
            error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            raise
        finally:
            _current_span.reset(token)
            event = TraceEvent(
                ts=datetime.now(UTC),
                run_id=self.run_id,
                span_id=span_id,
                parent_id=parent_id,
                kind=kind,
                name=name,
                status="error" if error else "ok",
                duration_ms=round((time.perf_counter() - start) * 1000, 3),
                error=error,
                attrs=handle.attrs,
                **handle.fields,
            )
            self._write(event)

    def write_blob(self, name: str, text: str) -> str:
        """Save a large payload to disk. Returns a path relative to base_dir."""
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:60] or "blob"
        rel = Path(self.run_id) / "blobs" / f"{next(self._blob_seq):04d}_{safe}.txt"
        try:
            (self.base_dir / rel).write_text(text, encoding="utf-8")
        except OSError:
            logger.warning("trace blob write failed: %s", rel, exc_info=True)
        return rel.as_posix()

    def _write(self, event: TraceEvent) -> None:
        line = event.model_dump_json() + "\n"
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            logger.warning("trace write failed: %s", self.path, exc_info=True)


def read_events(path: Path) -> list[TraceEvent]:
    """Load a trace file. Used by tests now, and by cost/eval aggregation later."""
    with open(path, encoding="utf-8") as f:
        return [TraceEvent.model_validate(json.loads(line)) for line in f if line.strip()]