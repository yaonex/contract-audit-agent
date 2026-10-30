"""Vulnerability taxonomy: single source of truth for category ids.

The yaml is DATA (categories can change without touching code); this module loads and
validates it and answers queries about it.
"""
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator

from audit_agent.config import get_settings

FALLBACK_CATEGORY_ID = "other"


class Category(BaseModel):
    id: str
    name: str
    description: str
    swc: list[str] = []
    smartbugs: str | None = None
    shard: bool = False
    slither_detectors: list[str] = []


class Taxonomy(BaseModel):
    version: int
    categories: list[Category]

    @model_validator(mode="after")
    def _check_integrity(self) -> "Taxonomy":
        # One validator, checks in a fixed order, so error messages are predictable.
        ids = [c.id for c in self.categories]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate category ids: {sorted(dupes)}")
        owner: dict[str, str] = {}
        for c in self.categories:
            for check in c.slither_detectors:
                if check in owner:
                    raise ValueError(
                        f"slither detector '{check}' is mapped twice ({owner[check]}, {c.id})"
                    )
                owner[check] = c.id
        if FALLBACK_CATEGORY_ID not in ids:
            raise ValueError(f"the fallback category '{FALLBACK_CATEGORY_ID}' must exist")
        return self

    @property
    def ids(self) -> set[str]:
        return {c.id for c in self.categories}

    def by_id(self, category_id: str) -> Category:
        return next(c for c in self.categories if c.id == category_id)

    def shard_categories(self) -> list[Category]:
        return [c for c in self.categories if c.shard]

    def from_smartbugs(self, label: str) -> Category | None:
        return next((c for c in self.categories if c.smartbugs == label), None)

    def category_for_detector(self, check: str) -> Category:
        """Map a Slither detector name to a category; unknown detectors fall back to `other`."""
        for c in self.categories:
            if check in c.slither_detectors:
                return c
        return self.by_id(FALLBACK_CATEGORY_ID)


@lru_cache
def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load and validate the taxonomy. Cached per path."""
    path = path or get_settings().taxonomy_path
    with open(path, encoding="utf-8") as f:
        return Taxonomy.model_validate(yaml.safe_load(f))