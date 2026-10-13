"""Vulnerability taxonomy: single source of truth for category ids."""
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator

from audit_agent.config import get_settings


class Category(BaseModel):
    id: str
    name: str
    description: str
    swc: list[str] = []
    smartbugs: str | None = None
    shard: bool = False


class Taxonomy(BaseModel):
    version: int
    categories: list[Category]

    @model_validator(mode="after")
    def _ids_unique(self) -> "Taxonomy":
        ids = [c.id for c in self.categories]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate category ids: {sorted(dupes)}")
        return self

    @property
    def ids(self) -> set[str]:
        return {c.id for c in self.categories}

    def shard_categories(self) -> list[Category]:
        return [c for c in self.categories if c.shard]

    def from_smartbugs(self, label: str) -> Category | None:
        return next((c for c in self.categories if c.smartbugs == label), None)


@lru_cache
def load_taxonomy(path: Path | None = None) -> Taxonomy:
    """Load and validate the taxonomy. Cached per path."""
    path = path or get_settings().taxonomy_path
    with open(path, encoding="utf-8") as f:
        return Taxonomy.model_validate(yaml.safe_load(f))