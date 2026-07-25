"""Tests for the CLIP text-tower concept catalog (Issue #195).

The committed catalog must ship *unbuilt* (every vector null) — we never
fabricate CLIP vectors. The loader degrades gracefully and the centroid
helper averages only the concepts that actually have a built vector.
"""

from __future__ import annotations

import json

import pytest
from app.concept_catalog import (
    CATALOG_PATH,
    ConceptCatalog,
    load_catalog,
    query_vector_for_concepts,
)
from app.concept_lexicon import CONCEPTS
from app.models import PLAY_EMBEDDING_CLIP_DIM


def test_committed_catalog_ships_unbuilt() -> None:
    raw = json.loads(CATALOG_PATH.read_text())
    assert raw["dim"] == PLAY_EMBEDDING_CLIP_DIM
    assert raw["generated_at"] is None
    assert all(c["vector"] is None for c in raw["concepts"])
    # Real loader therefore reports the catalog as not built.
    cat = load_catalog()
    assert cat.is_built is False
    assert cat.vectors == {}


def test_committed_catalog_covers_every_lexicon_concept() -> None:
    raw = json.loads(CATALOG_PATH.read_text())
    catalog_ids = {c["concept_id"] for c in raw["concepts"]}
    lexicon_ids = {c.concept_id for c in CONCEPTS}
    # Every concept the query parser can recognise has a catalog phrase, so a
    # built catalog can ground any lexicon match.
    assert lexicon_ids <= catalog_ids


def test_committed_catalog_has_no_soccer_phrases() -> None:
    blob = json.dumps(json.loads(CATALOG_PATH.read_text())).lower()
    for soccer in ("offside", "4-4-2", "expected goals", "midfielder", "la liga", "clean sheet"):
        assert soccer not in blob


def _built(**vectors: list[float]) -> ConceptCatalog:
    return ConceptCatalog(
        model="clip-vitb32",
        dim=PLAY_EMBEDDING_CLIP_DIM,
        generated_at="2026-05-31T00:00:00+00:00",
        vectors=vectors,
    )


def test_query_vector_centroid_is_unit_norm() -> None:
    cat = _built(
        cover_3=[1.0] + [0.0] * (PLAY_EMBEDDING_CLIP_DIM - 1),
        trips=[0.0, 1.0] + [0.0] * (PLAY_EMBEDDING_CLIP_DIM - 2),
    )
    vec = query_vector_for_concepts(cat, ["cover_3", "trips"])
    assert vec is not None
    norm = sum(v * v for v in vec) ** 0.5
    assert norm == pytest.approx(1.0)


def test_query_vector_none_when_no_concept_built() -> None:
    cat = _built(cover_3=[1.0] + [0.0] * (PLAY_EMBEDDING_CLIP_DIM - 1))
    assert query_vector_for_concepts(cat, ["mesh", "smash"]) is None


def test_query_vector_skips_missing_concepts() -> None:
    cat = _built(cover_3=[1.0] + [0.0] * (PLAY_EMBEDDING_CLIP_DIM - 1))
    vec = query_vector_for_concepts(cat, ["cover_3", "not_in_catalog"])
    assert vec is not None
    assert len(vec) == PLAY_EMBEDDING_CLIP_DIM


def test_loader_ignores_bad_dim_vectors(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "concept_catalog.json"
    bad.write_text(
        json.dumps(
            {
                "model": "clip-vitb32",
                "dim": PLAY_EMBEDDING_CLIP_DIM,
                "generated_at": "x",
                "concepts": [
                    {"concept_id": "cover_3", "phrase": "p", "vector": [0.1] * 10},  # wrong dim
                    {
                        "concept_id": "trips",
                        "phrase": "p",
                        "vector": [0.2] * PLAY_EMBEDDING_CLIP_DIM,
                    },
                ],
            }
        )
    )
    monkeypatch.setattr("app.concept_catalog.CATALOG_PATH", bad)
    load_catalog.cache_clear()
    try:
        cat = load_catalog()
        assert "cover_3" not in cat.vectors  # bad dim dropped
        assert "trips" in cat.vectors
    finally:
        load_catalog.cache_clear()
