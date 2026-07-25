"""Tests for the offline concept-catalog encoder (Issue #195).

No CLIP weights, no network: ``build_catalog`` takes an injectable encode
function, so we exercise the shape, normalisation, soccer guard, and the fact
that the *committed* catalog ships unbuilt (vectors null) — never fabricated.
"""

from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from scripts.build_concept_catalog import (  # noqa: E402
    CLIP_DIM,
    build_catalog,
    load_doc,
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_COMMITTED_CATALOG = os.path.join(_REPO_ROOT, "backend", "app", "data", "concept_catalog.json")


def _doc(*phrases: str) -> dict:
    return {
        "model": "clip-vitb32",
        "dim": CLIP_DIM,
        "generated_at": None,
        "concepts": [
            {"concept_id": f"c{i}", "category": "concept", "phrase": p, "vector": None}
            for i, p in enumerate(phrases)
        ],
    }


def _stub_encoder(scale: float):
    # Deterministic non-unit vectors so we can prove L2-normalisation happens.
    def encode(phrases):
        return [[scale * (j + 1)] * CLIP_DIM for j, _ in enumerate(phrases)]

    return encode


def test_build_catalog_fills_unit_norm_vectors() -> None:
    out = build_catalog(_doc("american football cover 3", "american football mesh play"), _stub_encoder(3.0))
    assert out["generated_at"] is not None
    for c in out["concepts"]:
        assert c["vector"] is not None
        assert len(c["vector"]) == CLIP_DIM
        norm = math.sqrt(sum(v * v for v in c["vector"]))
        assert math.isclose(norm, 1.0, rel_tol=1e-6)


def test_build_catalog_rejects_soccer_phrase() -> None:
    with pytest.raises(ValueError, match="soccer"):
        build_catalog(_doc("a 4-4-2 with an offside trap"), _stub_encoder(1.0))


def test_build_catalog_rejects_wrong_dim() -> None:
    def bad_encode(phrases):
        return [[0.0] * 10 for _ in phrases]

    with pytest.raises(ValueError, match="512"):
        build_catalog(_doc("american football trips"), bad_encode)


def test_committed_catalog_ships_unbuilt_and_is_american_football() -> None:
    """The committed file must carry phrases but NO fabricated vectors."""
    doc = load_doc(__import__("pathlib").Path(_COMMITTED_CATALOG))
    assert doc["dim"] == CLIP_DIM
    assert doc["generated_at"] is None
    concepts = doc["concepts"]
    assert len(concepts) >= 40
    # Honest: every committed vector is null until a real CLIP build runs.
    assert all(c["vector"] is None for c in concepts)
    # American football only — no soccer phrasing slipped in.
    blob = json.dumps(doc).lower()
    for soccer in ("offside", "4-4-2", "expected goals", "midfielder", "la liga"):
        assert soccer not in blob
