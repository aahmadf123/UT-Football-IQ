"""Zero-shot concept search (Issue #144).

``GET /api/v1/concept-search?q=...`` lets a coach ask *"find me all the Mesh
concepts"* or *"every Cover 3 trips look"* **without labelling plays first and
without any Toledo-specific fine-tuning**.

Two complementary, clearly-labelled signals — both reusing infrastructure that
already exists, with **no new vector database** (per Issue #144 / #8 / #77):

1. **Grounded metadata match (``source = "metadata"``).** The query is parsed by
   the American-football concept lexicon (`app.concept_lexicon`) into structured
   predicates over the labels Football-IQ already trusts (``clips.label_data``
   formation / coverage, ``personnel_grouping``, ``field_zone``). These are real
   labels, so they are *not* experimental.

2. **Experimental embedding expansion (``source = "embedding"``).** When a
   promoted play-embedding model exists (Issue #8), the grounded matches seed a
   centroid and we run the *existing* pgvector cosine search to surface visually
   similar reps that may not carry the explicit label yet. These are **always**
   flagged ``is_experimental = true`` and the response envelope is marked
   ``approximate``. Toggle with ``CONCEPT_SEARCH_EMBEDDING_EXPANSION``.

The whole response is ``approximate: true`` — zero-shot concept → label mapping
is a heuristic until validated on corrected Toledo clips. The front end labels
results accordingly; nothing here promotes a concept to an official label (that
stays the coach-review path in `app.routers.concept_proposals`).

This is distinct from ``POST /api/v1/search/text``, which is the *future* CLIP
text-tower path and stays gated behind ``ENABLE_EMBEDDING_TEXT_SEARCH`` until an
encoder is wired. See ``docs/concept-search.md``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Text, and_, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.concept_lexicon import (
    ConceptDefinition,
    ConceptMatch,
    detect_soccer_terms,
    group_by_category,
    parse_query,
)
from app.config import get_settings
from app.database import get_db
from app.deps import require_coach_or_above
from app.models import Clip, ModelVersion, PlayEmbedding, SideOfBall, User
from app.routers.search import (
    SearchFilters,
    _candidate_clip_ids,
    _model_label,
    _resolve_production_model_version,
    _run_vector_search,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/concept-search", tags=["concept-search"])

# How many grounded clips seed the embedding-expansion centroid. Bounded so the
# endpoint stays cheap (it is not workload-gated, mirroring /search/*).
_SEED_CAP = 50


# ── Schemas ───────────────────────────────────────────────────────────────────


class MatchedConcept(BaseModel):
    concept_id: str
    display_name: str
    category: str
    confidence: float


class ConceptResult(BaseModel):
    model_config = {"protected_namespaces": ()}

    clip_id: uuid.UUID
    source: str  # "metadata" | "embedding"
    confidence: float
    score: float | None = None  # cosine score for embedding-source results
    is_experimental: bool
    matched_concept_ids: list[str]
    label_data: dict[str, Any] | None = None


class ConceptSearchResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    query: str
    matched_concepts: list[MatchedConcept]
    approximate: bool
    experimental: bool
    reason: str | None = None
    model_version_label: str | None = None
    results: list[ConceptResult]


# ── Predicate building ────────────────────────────────────────────────────────


def _concept_predicate(concept: ConceptDefinition) -> ColumnElement[bool]:
    """SQLAlchemy boolean for one concept over the existing ``clips`` columns.

    Formations / coverages use ``ConceptDefinition.json_path`` for precise JSON
    matching, plus a serialized ``label_data`` fallback for tolerance when a
    concept is stored under a non-canonical key. Concepts grounded on first-class
    clip columns (``personnel_grouping`` / ``field_zone``) match those columns
    only, avoiding broad JSON-text false positives (e.g. personnel digits).
    """
    label_text = cast(Clip.label_data, Text)
    clauses: list[ColumnElement[bool]] = []
    for term in concept.terms():
        pattern = f"%{term}%"
        if concept.column == "personnel_grouping":
            clauses.append(Clip.personnel_grouping.ilike(pattern))
            continue
        if concept.column == "field_zone":
            clauses.append(Clip.field_zone.ilike(pattern))
            continue
        if concept.json_path is not None:
            path_l1, path_l2 = concept.json_path
            clauses.append(cast(Clip.label_data[path_l1][path_l2], Text).ilike(pattern))
        clauses.append(label_text.ilike(pattern))
    return or_(*clauses)


def _grounded_predicate(matches: list[ConceptMatch]) -> ColumnElement[bool]:
    """OR within a concept category, AND across categories.

    "trips cover 3" → (trips formations) AND (cover-3 coverages). "cover 2 or
    cover 3" → both are coverages, so they OR and a clip matching either counts.
    """
    grouped = group_by_category(matches)
    per_category: list[ColumnElement[bool]] = []
    for cat_matches in grouped.values():
        per_category.append(or_(*[_concept_predicate(m.concept) for m in cat_matches]))
    return and_(*per_category)


def _centroid(vectors: list[list[float]]) -> list[float] | None:
    """L2-normalized mean of the seed embeddings (pure Python, no numpy dep)."""
    usable = [v for v in vectors if v]
    if not usable:
        return None
    dim = len(usable[0])
    if any(len(v) != dim for v in usable):
        return None
    sums = [0.0] * dim
    for v in usable:
        for i, x in enumerate(v):
            sums[i] += x
    mean = [s / len(usable) for s in sums]
    norm = sum(x * x for x in mean) ** 0.5
    if norm == 0.0:
        return None
    return [x / norm for x in mean]


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("", response_model=ConceptSearchResponse)
async def concept_search(
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
    q: str = Query(..., min_length=1, max_length=256, description="Free-text concept query"),
    k: int = Query(default=20, ge=1, le=50),
    include_experimental: bool = Query(default=True),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    opponent: str | None = Query(default=None),
    side_of_ball: SideOfBall | None = Query(default=None),
) -> ConceptSearchResponse:
    """Zero-shot, metadata-grounded concept search with optional embedding expansion.

    Results are always *approximate* (zero-shot concept→label mapping). Embedding
    expansion results are additionally *experimental*. Coaches only — ``player`` /
    ``viewer`` are blocked by ``require_coach_or_above``.
    """
    matches = parse_query(q)
    matched_concepts = [
        MatchedConcept(
            concept_id=m.concept.concept_id,
            display_name=m.concept.display_name,
            category=m.concept.category.value,
            confidence=m.confidence,
        )
        for m in matches
    ]

    if not matches:
        soccer = detect_soccer_terms(q)
        reason = (
            "This looks like soccer / association football — Football-IQ is American football only."
            if soccer
            else "No recognized American-football concept in the query."
        )
        log.info("concept_search_no_match", soccer_terms=len(soccer))
        return ConceptSearchResponse(
            query=q,
            matched_concepts=[],
            approximate=True,
            experimental=False,
            reason=reason,
            model_version_label=None,
            results=[],
        )

    filters = SearchFilters(since=since, until=until, opponent=opponent, side_of_ball=side_of_ball)
    candidate_ids = await _candidate_clip_ids(db, filters)

    # ── 1. Grounded metadata matches ──────────────────────────────────────
    grounded_q = (
        select(Clip.id, Clip.label_data, Clip.confidence)
        .where(_grounded_predicate(matches))
        .order_by(Clip.created_at.desc())
        .limit(k)
    )
    if candidate_ids is not None:
        if not candidate_ids:
            grounded_rows: list[Any] = []
        else:
            grounded_q = grounded_q.where(Clip.id.in_(candidate_ids))
            grounded_rows = list((await db.execute(grounded_q)).all())
    else:
        grounded_rows = list((await db.execute(grounded_q)).all())

    concept_ids = [m.concept.concept_id for m in matches]
    parse_conf = round(sum(m.confidence for m in matches) / len(matches), 3)

    results: list[ConceptResult] = []
    seen: set[uuid.UUID] = set()
    seed_clip_ids: list[uuid.UUID] = []
    for row in grounded_rows:
        clip_id = row[0]
        seen.add(clip_id)
        seed_clip_ids.append(clip_id)
        results.append(
            ConceptResult(
                clip_id=clip_id,
                source="metadata",
                confidence=parse_conf,
                score=None,
                is_experimental=False,
                matched_concept_ids=concept_ids,
                label_data=row[1],
            )
        )

    # ── 2. Experimental embedding expansion (optional) ────────────────────
    settings = get_settings()
    mv: ModelVersion | None = None
    experimental = False
    if (
        settings.concept_search_embedding_expansion
        and include_experimental
        and seed_clip_ids
        and len(results) < k
    ):
        mv = await _resolve_production_model_version(db)
        if mv is not None:
            seed_vectors = await _load_seed_vectors(db, seed_clip_ids[:_SEED_CAP], mv.id)
            centroid = _centroid(seed_vectors)
            if centroid is not None:
                expansion = await _run_vector_search(
                    db,
                    anchor_vector=centroid,
                    k=k,
                    chunk_kind="play",
                    model_version_id=mv.id,
                    candidate_clip_ids=candidate_ids,
                    include_experimental=True,
                    exclude_clip_ids=list(seen),
                )
                for r in expansion:
                    if r.clip_id in seen:
                        continue
                    seen.add(r.clip_id)
                    experimental = True
                    similarity = max(-1.0, min(1.0, r.score))
                    results.append(
                        ConceptResult(
                            clip_id=r.clip_id,
                            source="embedding",
                            confidence=round((similarity + 1.0) / 2.0, 3),
                            score=round(similarity, 3),
                            is_experimental=True,
                            matched_concept_ids=concept_ids,
                            label_data=r.label_data,
                        )
                    )

    log.info(
        "concept_search",
        concept_count=len(matches),
        grounded=len(seed_clip_ids),
        total_results=len(results),
        expanded=experimental,
    )
    return ConceptSearchResponse(
        query=q,
        matched_concepts=matched_concepts,
        approximate=True,
        experimental=experimental,
        reason=None if results else "No clips match this concept yet.",
        model_version_label=_model_label(mv) if mv is not None else None,
        results=results[:k],
    )


async def _load_seed_vectors(
    db: AsyncSession, clip_ids: list[uuid.UUID], model_version_id: uuid.UUID
) -> list[list[float]]:
    """Fetch the play-embedding vectors for the grounded seed clips."""
    rows = await db.execute(
        select(PlayEmbedding.vector).where(
            PlayEmbedding.clip_id.in_(clip_ids),
            PlayEmbedding.model_version_id == model_version_id,
            PlayEmbedding.chunk_kind == "play",
        )
    )
    return [list(v) for (v,) in rows.all() if v is not None]
