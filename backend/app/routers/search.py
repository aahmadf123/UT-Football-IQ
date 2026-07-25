"""Similar-rep search router (Issue #8 — Phase 3).

Endpoints:

* ``POST /api/v1/search/similar`` — given a clip ID, return the top-N
  clips whose play embeddings are nearest by cosine similarity. Supports
  the design-doc filter set: date range, opponent (via
  ``videos.metadata``), formation, coverage, side-of-ball.

* ``POST /api/v1/search/vector`` — same retrieval shape but the request
  supplies a raw 256-d vector. Useful for batch CLI tools that want to
  search by a centroid or hand-crafted prototype.

* ``POST /api/v1/search/text`` — *experimental* CLIP text-tower search
  (Issue #195). Encodes the query into CLIP's shared text-image space —
  via an injected CLIP text tower or the precomputed concept catalog —
  and cosine-searches it against the raw 512-d ``clip_vector`` (the CLIP
  *image* embedding). Outputs always carry ``experimental: true`` /
  ``approximate: true`` and are filtered out of any caller that opts into
  production-only results. The endpoint refuses to serve unless
  ``ENABLE_EMBEDDING_TEXT_SEARCH`` is truthy in the env so the surface
  stays gated end-to-end.

The vector similarity is computed in Postgres via the pgvector ``<=>``
cosine-distance operator. We pre-filter on label / date / opponent
fields (selective WHEREs) and let pgvector's ivfflat index rank the
candidate set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.concept_catalog import load_catalog, query_vector_for_concepts
from app.concept_lexicon import detect_soccer_terms, parse_query
from app.database import get_db
from app.deps import get_current_user
from app.models import (
    PLAY_EMBEDDING_CLIP_DIM,
    PLAY_EMBEDDING_DIM,
    Clip,
    ModelStage,
    ModelVersion,
    PlayEmbedding,
    SessionKind,
    SideOfBall,
    User,
    Video,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/search", tags=["search"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class SearchFilters(BaseModel):
    """Coach-facing filter set; all fields optional."""

    since: datetime | None = None
    until: datetime | None = None
    opponent: str | None = None
    formation: str | None = None
    coverage: str | None = None
    side_of_ball: SideOfBall | None = None
    session_kind: SessionKind | None = None
    our_possession: SideOfBall | None = None
    practice_session_id: uuid.UUID | None = None
    include_experimental: bool = False


class SimilarSearchRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    clip_id: uuid.UUID
    k: int = Field(default=20, ge=1, le=200)
    chunk_kind: str = "play"
    filters: SearchFilters = Field(default_factory=SearchFilters)


class VectorSearchRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    vector: list[float] = Field(..., min_length=PLAY_EMBEDDING_DIM, max_length=PLAY_EMBEDDING_DIM)
    k: int = Field(default=20, ge=1, le=200)
    chunk_kind: str = "play"
    model_version_id: uuid.UUID | None = None
    filters: SearchFilters = Field(default_factory=SearchFilters)


class TextSearchRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    query: str = Field(..., min_length=1, max_length=512)
    k: int = Field(default=20, ge=1, le=200)
    chunk_kind: str = "play"
    filters: SearchFilters = Field(default_factory=SearchFilters)


class SimilarResult(BaseModel):
    model_config = {"protected_namespaces": ()}

    clip_id: uuid.UUID
    embedding_id: uuid.UUID
    score: float
    is_experimental: bool
    snap_anchor: bool
    chunk_kind: str
    label_data: dict[str, Any] | None = None


class SearchResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    anchor_clip_id: uuid.UUID | None
    model_version_id: uuid.UUID | None
    model_version_label: str | None
    experimental: bool
    reason: str | None = None
    results: list[SimilarResult]
    # ── CLIP text-tower search annotations (Issue #195) ──────────────────────
    # ``approximate`` flags zero-shot text→image matching; ``query`` /
    # ``matched_concept_ids`` echo how a free-text query was grounded. Default
    # to the non-text shape so ``/similar`` and ``/vector`` envelopes are
    # unchanged.
    approximate: bool = False
    query: str | None = None
    matched_concept_ids: list[str] = Field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────


PRODUCTION_STAGES = (ModelStage.production, ModelStage.staging)


async def _resolve_production_model_version(
    db: AsyncSession,
) -> ModelVersion | None:
    """Pick the highest-promoted embedding model version available.

    Preference order: production > staging. Among rows of the same stage
    we pick the most recently created. ``experimental`` versions never
    serve the retrieval router by default — they exist to write rows,
    not to be searched against.
    """
    q = (
        select(ModelVersion)
        .where(ModelVersion.model_type == "play_embedding")
        .where(ModelVersion.promoted_stage.in_(PRODUCTION_STAGES))
        .order_by(ModelVersion.promoted_stage.asc(), ModelVersion.created_at.desc())
    )
    result = await db.execute(q)
    versions = list(result.scalars().all())
    # Prefer production over staging when both exist.
    for stage in PRODUCTION_STAGES:
        for v in versions:
            if v.promoted_stage == stage:
                return v
    return versions[0] if versions else None


def _model_label(mv: ModelVersion) -> str:
    return f"{mv.model_name}@{mv.version}"


async def _candidate_clip_ids(db: AsyncSession, filters: SearchFilters) -> list[uuid.UUID] | None:
    """Pre-filter clip IDs by metadata. Returns ``None`` if no filters set."""
    if not any(
        [
            filters.since,
            filters.until,
            filters.opponent,
            filters.formation,
            filters.coverage,
            filters.side_of_ball,
            filters.session_kind,
            filters.our_possession,
            filters.practice_session_id,
        ]
    ):
        return None

    q = select(Clip.id).join(Video, Video.id == Clip.video_id)
    if filters.since is not None:
        q = q.where(Video.recorded_at >= filters.since)
    if filters.until is not None:
        q = q.where(Video.recorded_at <= filters.until)
    if filters.opponent:
        # Opponent first-class column wins, but the JSON value remains
        # a compatibility fallback (#98 backfill is best-effort).
        q = q.where(
            or_(
                Video.opponent_team == filters.opponent,
                Video.metadata_["opponent"].astext == filters.opponent,
            )
        )
    if filters.formation:
        q = q.where(Clip.label_data["formation"]["generic"].astext.ilike(filters.formation))
    if filters.coverage:
        q = q.where(Clip.label_data["coverage"]["generic"].astext.ilike(filters.coverage))
    if filters.side_of_ball:
        q = q.where(Clip.side_of_ball == filters.side_of_ball)
    if filters.session_kind:
        q = q.where(Video.session_kind == filters.session_kind)
    if filters.our_possession:
        q = q.where(Clip.our_possession == filters.our_possession)
    if filters.practice_session_id:
        q = q.where(Video.practice_session_id == filters.practice_session_id)

    result = await db.execute(q)
    return [r[0] for r in result.all()]


def _format_vector_for_sql(values: list[float]) -> str:
    """pgvector accepts ``"[1.0,2.0,...]"`` as a parameter binding."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# The only pgvector columns the search SQL is ever allowed to rank on.
# ``_run_vector_search`` interpolates the column name into the query, so it
# must come from this closed set — never from request input.
_VECTOR_COLUMNS: frozenset[str] = frozenset({"vector", "clip_vector"})


async def _run_vector_search(
    db: AsyncSession,
    *,
    anchor_vector: list[float],
    k: int,
    chunk_kind: str,
    model_version_id: uuid.UUID,
    candidate_clip_ids: list[uuid.UUID] | None,
    include_experimental: bool,
    exclude_clip_ids: list[uuid.UUID] | None = None,
    vector_column: str = "vector",
) -> list[SimilarResult]:
    """Run the pgvector cosine-distance ORDER BY for the given anchor.

    ``vector_column`` selects which pgvector column to rank on: the fused
    256-d ``vector`` (similar / concept search) or the raw 512-d
    ``clip_vector`` in CLIP shared space (CLIP text-tower search, Issue
    #195). Rows whose ranked column is NULL are excluded, so a
    ``clip_vector`` search naturally skips embeddings produced without a
    real CLIP encoder.
    """
    if vector_column not in _VECTOR_COLUMNS:
        raise ValueError(f"unsupported vector column: {vector_column!r}")
    where_clauses = [
        "pe.model_version_id = :model_version_id",
        "pe.chunk_kind = :chunk_kind",
        f"pe.{vector_column} IS NOT NULL",
    ]
    params: dict[str, Any] = {
        "anchor": _format_vector_for_sql(anchor_vector),
        "k": k,
        "model_version_id": str(model_version_id),
        "chunk_kind": chunk_kind,
    }
    if not include_experimental:
        where_clauses.append("pe.is_experimental = false")
    if candidate_clip_ids is not None:
        if not candidate_clip_ids:
            return []
        where_clauses.append("pe.clip_id = ANY(:candidate_clip_ids)")
        params["candidate_clip_ids"] = [str(cid) for cid in candidate_clip_ids]
    if exclude_clip_ids:
        where_clauses.append("pe.clip_id <> ALL(:exclude_clip_ids)")
        params["exclude_clip_ids"] = [str(cid) for cid in exclude_clip_ids]

    # The ``where_clauses`` list and ``vector_column`` are built from a closed
    # set of literal fragments in this function — no user input ever lands in
    # them (``vector_column`` is validated against ``_VECTOR_COLUMNS`` above) —
    # so the f-string interpolation is safe and the bandit warning is a false
    # positive. All actual values flow through ``params`` and are bound by
    # SQLAlchemy as parameters.
    where_sql = " AND ".join(where_clauses)
    sql_str = (
        "SELECT pe.id AS embedding_id, pe.clip_id AS clip_id, "  # noqa: S608
        "pe.snap_anchor AS snap_anchor, pe.chunk_kind AS chunk_kind, "
        "pe.is_experimental AS is_experimental, c.label_data AS label_data, "
        f"1 - (pe.{vector_column} <=> CAST(:anchor AS vector)) AS score "
        "FROM playembeddings pe "
        "JOIN clips c ON c.id = pe.clip_id "
        f"WHERE {where_sql} "
        f"ORDER BY pe.{vector_column} <=> CAST(:anchor AS vector) ASC "
        "LIMIT :k"
    )
    from sqlalchemy.dialects.postgresql import ARRAY, UUID

    sql = text(sql_str)
    if candidate_clip_ids is not None:
        sql = sql.bindparams(bindparam("candidate_clip_ids", type_=ARRAY(UUID(as_uuid=True))))
    if exclude_clip_ids:
        sql = sql.bindparams(bindparam("exclude_clip_ids", type_=ARRAY(UUID(as_uuid=True))))
    result = await db.execute(sql, params)
    out: list[SimilarResult] = []
    for row in result.mappings():
        out.append(
            SimilarResult(
                clip_id=row["clip_id"],
                embedding_id=row["embedding_id"],
                score=float(row["score"]),
                is_experimental=bool(row["is_experimental"]),
                snap_anchor=bool(row["snap_anchor"]),
                chunk_kind=row["chunk_kind"],
                label_data=row["label_data"],
            )
        )
    return out


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/similar", response_model=SearchResponse)
async def search_similar(
    payload: SimilarSearchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> SearchResponse:
    """Top-K plays whose embedding is closest to the anchor clip's.

    The router automatically resolves the production / staging embedding
    model version and excludes the anchor clip itself from the result
    list. Filters (opponent, date range, formation, coverage,
    side-of-ball) are applied as a pre-filter before the vector ORDER BY
    so pgvector only ranks the relevant candidate set.
    """
    mv = await _resolve_production_model_version(db)
    if mv is None:
        return SearchResponse(
            anchor_clip_id=payload.clip_id,
            model_version_id=None,
            model_version_label=None,
            experimental=False,
            reason="no production embedding model",
            results=[],
        )

    anchor_q = select(PlayEmbedding).where(
        PlayEmbedding.clip_id == payload.clip_id,
        PlayEmbedding.chunk_kind == payload.chunk_kind,
        PlayEmbedding.model_version_id == mv.id,
    )
    anchor = (await db.execute(anchor_q)).scalar_one_or_none()
    if anchor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No embedding for this clip with the production model version yet — "
                "rerun the nightly embed stage."
            ),
        )

    candidate_ids = await _candidate_clip_ids(db, payload.filters)
    results = await _run_vector_search(
        db,
        anchor_vector=list(anchor.vector or []),
        k=payload.k,
        chunk_kind=payload.chunk_kind,
        model_version_id=mv.id,
        candidate_clip_ids=candidate_ids,
        include_experimental=payload.filters.include_experimental,
        exclude_clip_ids=[payload.clip_id],
    )
    log.info(
        "search_similar",
        anchor_clip_id=str(payload.clip_id),
        k=payload.k,
        result_count=len(results),
        model_version_id=str(mv.id),
    )
    return SearchResponse(
        anchor_clip_id=payload.clip_id,
        model_version_id=mv.id,
        model_version_label=_model_label(mv),
        experimental=False,
        results=results,
    )


@router.post("/vector", response_model=SearchResponse)
async def search_by_vector(
    payload: VectorSearchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> SearchResponse:
    """Top-K plays nearest a raw query vector.

    The query model version is the one supplied in the request, falling
    back to the highest promoted ``play_embedding`` version. Useful for
    batch / CLI tools that want to query by a hand-built prototype or a
    cluster centroid pulled from ``embedding_cluster_proposals``.
    """
    if payload.model_version_id is not None:
        mv = (
            await db.execute(
                select(ModelVersion).where(ModelVersion.id == payload.model_version_id)
            )
        ).scalar_one_or_none()
    else:
        mv = await _resolve_production_model_version(db)
    if mv is None:
        return SearchResponse(
            anchor_clip_id=None,
            model_version_id=None,
            model_version_label=None,
            experimental=False,
            reason="no embedding model version available",
            results=[],
        )

    candidate_ids = await _candidate_clip_ids(db, payload.filters)
    results = await _run_vector_search(
        db,
        anchor_vector=payload.vector,
        k=payload.k,
        chunk_kind=payload.chunk_kind,
        model_version_id=mv.id,
        candidate_clip_ids=candidate_ids,
        include_experimental=payload.filters.include_experimental,
    )
    log.info(
        "search_by_vector",
        k=payload.k,
        result_count=len(results),
        model_version_id=str(mv.id),
    )
    return SearchResponse(
        anchor_clip_id=None,
        model_version_id=mv.id,
        model_version_label=_model_label(mv),
        experimental=False,
        results=results,
    )


def _text_search_enabled() -> bool:
    raw = os.environ.get("ENABLE_EMBEDDING_TEXT_SEARCH", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Type of an injected CLIP text encoder: free text → 512-d CLIP text vector.
TextEncoder = Callable[[str], list[float]]


def _resolve_text_encoder(request: Request) -> TextEncoder | None:
    """Return the deployment-injected CLIP text encoder, if any.

    Production may mount a CPU CLIP **text** tower at startup
    (``app.state.clip_text_encoder``) to encode *arbitrary* free-text
    queries. When absent we fall back to the precomputed concept catalog,
    which only needs committed vectors — no CLIP weights in the container.
    """
    encoder: Any = getattr(request.app.state, "clip_text_encoder", None)
    if callable(encoder):
        return cast(TextEncoder, encoder)
    return None


def _text_envelope(
    query: str,
    *,
    reason: str | None,
    results: list[SimilarResult],
    mv: ModelVersion | None = None,
    matched_concept_ids: list[str] | None = None,
) -> SearchResponse:
    """Build the always-experimental/approximate ``/search/text`` envelope."""
    return SearchResponse(
        anchor_clip_id=None,
        model_version_id=mv.id if mv else None,
        model_version_label=_model_label(mv) if mv else None,
        experimental=True,
        approximate=True,
        reason=reason,
        results=results,
        query=query,
        matched_concept_ids=matched_concept_ids or [],
    )


@router.post("/text", response_model=SearchResponse)
async def search_by_text(
    payload: TextSearchRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> SearchResponse:
    """Genuine CLIP text-tower zero-shot search — *experimental* (Issue #195).

    Encodes the query into CLIP's shared text-image space and cosine-searches
    it against the raw ``playembeddings.clip_vector`` (the 512-d CLIP image
    embedding). Two ways to get the query vector, in order:

    1. a deployment-injected CLIP **text** tower (``app.state.clip_text_encoder``)
       — handles arbitrary phrasing; or
    2. the precomputed, committed **concept catalog** (``app.concept_catalog``):
       the query is grounded to American-football concept(s) lexically via the
       Issue #144 lexicon (soccer rejected), and those concepts' precomputed
       CLIP text vectors are averaged — no CLIP weights in the backend.

    Results **always** carry ``experimental: true`` / ``approximate: true`` so
    the front-end labels them and the correction-export job skips them; nothing
    here promotes to an official label. The surface stays gated behind
    ``ENABLE_EMBEDDING_TEXT_SEARCH`` (503 when off) and never silently degrades
    to similar-by-clip. When neither an encoder nor a built catalog can ground
    the query, it returns a 200 envelope with a clear ``reason`` and no results
    — it never fabricates matches.
    """
    if not _text_search_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Text search is gated behind ENABLE_EMBEDDING_TEXT_SEARCH and is "
                "currently disabled."
            ),
        )

    query = payload.query

    # American-football only: a "football" query that is really about soccer is
    # rejected with a clear reason rather than silently matching nothing.
    soccer_hits = detect_soccer_terms(query)
    if soccer_hits:
        return _text_envelope(
            query,
            reason=(
                "Football-IQ is American football only — this query looks like "
                f"soccer / association football ({', '.join(soccer_hits)})."
            ),
            results=[],
        )

    encoder = _resolve_text_encoder(request)

    # Ground the query to lexicon concepts (used for the catalog path and for
    # transparency in the response either way).
    matches = parse_query(query)
    matched_concept_ids = [m.concept.concept_id for m in matches]

    # Build the query vector in CLIP space.
    query_vector: list[float] | None = None
    if encoder is not None:
        try:
            raw = list(encoder(query))
        except Exception:  # pragma: no cover - defensive; encoder is deployment code
            log.warning("clip_text_encoder_failed", query_len=len(query))
            raw = []
        if len(raw) == PLAY_EMBEDDING_CLIP_DIM:
            query_vector = [float(x) for x in raw]
        else:
            log.warning("clip_text_encoder_bad_dim", got=len(raw), expected=PLAY_EMBEDDING_CLIP_DIM)
    if query_vector is None:
        # Catalog fallback: need at least one grounded concept with a vector.
        if not matched_concept_ids:
            return _text_envelope(
                query,
                reason=(
                    "Could not ground the query to an American-football concept. "
                    "Try a concept like 'cover 3', 'trips', 'mesh', or 'jet sweep'."
                ),
                results=[],
            )
        catalog = load_catalog()
        query_vector = query_vector_for_concepts(catalog, matched_concept_ids)
        if query_vector is None:
            return _text_envelope(
                query,
                reason=(
                    "Text-search concept catalog is not built yet — run the "
                    "offline CLIP text-tower encoder "
                    "(gpu-worker/scripts/build_concept_catalog.py) or inject a "
                    "CLIP text encoder."
                ),
                results=[],
                matched_concept_ids=matched_concept_ids,
            )

    mv = await _resolve_production_model_version(db)
    if mv is None:
        return _text_envelope(
            query,
            reason="no production embedding model",
            results=[],
            matched_concept_ids=matched_concept_ids,
        )

    candidate_ids = await _candidate_clip_ids(db, payload.filters)
    results = await _run_vector_search(
        db,
        anchor_vector=query_vector,
        k=payload.k,
        chunk_kind=payload.chunk_kind,
        model_version_id=mv.id,
        candidate_clip_ids=candidate_ids,
        # Text results are inherently experimental — include the experimental
        # clip_vector rows rather than filtering them out.
        include_experimental=True,
        vector_column="clip_vector",
    )
    # Every text result is experimental/approximate regardless of the row's
    # own flag (the row's clip_vector is image-derived; the *text* match is
    # the heuristic).
    for r in results:
        r.is_experimental = True
    log.info(
        "search_by_text",
        k=payload.k,
        result_count=len(results),
        model_version_id=str(mv.id),
        used_encoder=encoder is not None,
        matched_concepts=len(matched_concept_ids),
    )
    return _text_envelope(
        query,
        reason=None,
        results=results,
        mv=mv,
        matched_concept_ids=matched_concept_ids,
    )
