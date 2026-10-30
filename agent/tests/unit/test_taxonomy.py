import pytest

from pathlib import Path

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


SLITHER_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "slither-0.11.6"


def _real_detector_names() -> set[str]:
    """Parse the table printed by `slither --list-detectors` (checked in as a fixture)."""
    names: set[str] = set()
    for line in (SLITHER_FIXTURES / "detectors.txt").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0].isdigit():
            names.add(cells[1])
    return names


def test_slither_mapping_lookup():
    t = load_taxonomy()
    assert t.category_for_detector("reentrancy-eth").id == "reentrancy"
    assert t.category_for_detector("tx-origin").id == "access_control"
    assert t.category_for_detector("missing-zero-check").id == "other"   # NOT access_control
    assert t.category_for_detector("no-such-detector").id == "other"


def test_every_mapped_detector_exists_in_the_real_slither_list():
    real = _real_detector_names()
    assert len(real) > 50                      # sanity: the table really was parsed
    mapped = {d for c in load_taxonomy().categories for d in c.slither_detectors}
    assert not (mapped - real), f"not in the slither-0.11.6 list: {sorted(mapped - real)}"


def _cats(*extra):
    base = [{"id": "other", "name": "O", "description": "x"}]
    return {"version": 1, "categories": [*base, *extra]}


def test_detector_mapped_to_two_categories_is_rejected():
    data = _cats(
        {"id": "a", "name": "A", "description": "x", "slither_detectors": ["d1"]},
        {"id": "b", "name": "B", "description": "x", "slither_detectors": ["d1"]},
    )
    with pytest.raises(ValueError, match="mapped twice"):
        Taxonomy.model_validate(data)


def test_taxonomy_requires_the_fallback_category():
    with pytest.raises(ValueError, match="fallback"):
        Taxonomy.model_validate(
            {"version": 1, "categories": [{"id": "a", "name": "A", "description": "x"}]}
        )