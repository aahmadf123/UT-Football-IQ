"""Precomputed CLIP text-tower concept catalog (Issue #195).

Genuine CLIP text-tower zero-shot search needs to turn a coach's free-text
query into a vector *in CLIP's shared text-image space* so it can be
cosine-compared against the raw ``playembeddings.clip_vector`` (the 512-d
CLIP image embedding). Running the CLIP text tower inside the backend
container would mean shipping CLIP weights there, which we explicitly avoid
(see ``docs/embeddings-architecture.md`` §7 and the SAM/PARSeq precedent of
never committing weights).

Instead we precompute, **offline**, the CLIP text-tower embedding of a small
curated **American-football-only** concept vocabulary and commit the
resulting vectors as a tiny JSON file (vectors, *not* weights). At query
time the backend:

1. grounds the query to lexicon concept(s) lexically via
   ``app.concept_lexicon`` (reuses Issue #144; American-football only, soccer
   rejected) — **no text encoder needed**;
2. looks up each matched concept's precomputed CLIP text vector here;
3. averages them into a single query vector and cosine-searches
   ``clip_vector``.

The committed catalog ships with ``vector: null`` for every concept until the
offline encoder (``gpu-worker/scripts/build_concept_catalog.py``) is run with
real CLIP weights and the populated file is committed. Until then
``/api/v1/search/text`` honestly reports the catalog is unbuilt rather than
returning fabricated matches — no mock data is ever presented as real
results.

This module is pure logic + a JSON read: no database, no model weights, no
network. The catalog phrases (the vocabulary) are derived once from the
lexicon, so the committed file stays in sync with the concepts the query
parser can recognise.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import structlog

from app.models import PLAY_EMBEDDING_CLIP_DIM

log = structlog.get_logger(__name__)

# Committed catalog file. Phrases are real curated American-football prompts;
# vectors are null until the offline CLIP encoder fills and re-commits them.
CATALOG_PATH: Path = Path(__file__).resolve().parent / "data" / "concept_catalog.json"


@dataclass(frozen=True)
class ConceptCatalog:
    """Parsed concept catalog: ``concept_id -> CLIP text vector (512-d)``.

    Only concepts whose offline-encoded vector is present appear in
    ``vectors``; concepts still awaiting an encode are omitted, so an empty
    ``vectors`` mapping means "catalog not built yet".
    """

    model: str
    dim: int
    generated_at: str | None
    vectors: dict[str, list[float]]

    @property
    def is_built(self) -> bool:
        return bool(self.vectors)


def _l2_normalise(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return list(values)
    return [v / norm for v in values]


@lru_cache(maxsize=1)
def load_catalog() -> ConceptCatalog:
    """Load and cache the committed CLIP text-tower concept catalog.

    Never raises on a missing/malformed/empty file — returns an unbuilt
    catalog (empty ``vectors``) so ``/search/text`` degrades to a clear
    "catalog not built" response instead of crashing.
    """
    try:
        raw = json.loads(CATALOG_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("concept_catalog_unreadable", path=str(CATALOG_PATH), error=str(exc))
        return ConceptCatalog(
            model="clip-vitb32", dim=PLAY_EMBEDDING_CLIP_DIM, generated_at=None, vectors={}
        )

    dim = int(raw.get("dim", PLAY_EMBEDDING_CLIP_DIM))
    vectors: dict[str, list[float]] = {}
    for entry in raw.get("concepts", []):
        concept_id = entry.get("concept_id")
        vec = entry.get("vector")
        if not concept_id or vec is None:
            continue
        if not isinstance(vec, list) or len(vec) != dim:
            log.warning("concept_catalog_bad_vector", concept_id=concept_id, dim=dim)
            continue
        vectors[concept_id] = _l2_normalise([float(x) for x in vec])

    return ConceptCatalog(
        model=str(raw.get("model", "clip-vitb32")),
        dim=dim,
        generated_at=raw.get("generated_at"),
        vectors=vectors,
    )


def query_vector_for_concepts(
    catalog: ConceptCatalog, concept_ids: list[str]
) -> list[float] | None:
    """Average the catalog text vectors for ``concept_ids`` into one query vector.

    Returns the L2-normalised centroid of the matched concepts' CLIP text
    vectors, or ``None`` when none of the concepts have a built vector (the
    caller then reports the catalog is not built for these concepts).
    """
    present = [catalog.vectors[c] for c in concept_ids if c in catalog.vectors]
    if not present:
        return None
    dim = len(present[0])
    centroid = [0.0] * dim
    for vec in present:
        for i, value in enumerate(vec):
            centroid[i] += value
    centroid = [v / len(present) for v in centroid]
    return _l2_normalise(centroid)
