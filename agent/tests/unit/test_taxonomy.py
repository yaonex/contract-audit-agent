import pytest

from audit_agent.schemas.taxonomy import Taxonomy, load_taxonomy


def test_real_taxonomy_loads():
    t = load_taxonomy()  # reads knowledge/taxonomy.yaml (cwd = repo root, via make)
    assert "reentrancy" in t.ids
    assert len(t.shard_categories()) >= 1


def test_smartbugs_lookup():
    t = load_taxonomy()
    result = t.from_smartbugs("reentrancy")
    assert result is not None
    assert result.id == "reentrancy"
    assert t.from_smartbugs("no_such_label") is None


def test_duplicate_ids_rejected():
    data = {
        "version": 1,
        "categories": [
            {"id": "a", "name": "A", "description": "x"},
            {"id": "a", "name": "A2", "description": "y"},
        ],
    }
    with pytest.raises(ValueError, match="duplicate"):
        Taxonomy.model_validate(data)
