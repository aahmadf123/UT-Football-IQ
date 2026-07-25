"""Playbook overlays & assignment execution scoring (Issue #15).

Turns the call sheet into a Film Room overlay layer: coaches author concepts
(intended routes, responsibilities, coaching points), link a concept to the clip
it was called on, and get a transparent, rule-based execution grade per
assignment and per play. Every grade carries confidence/uncertainty and reason
codes, degrades to ``needs_review`` when the CV substrate is missing or the
player identity is shaky, and is overridable by a coach (the override is stored
as an ``assignment_execution`` training label).

Boundaries this router respects:

* **Single camera** (#101): overlay geometry is single-camera, frame-normalized;
  there is no multi-angle camera switching.
* **No new vector store** (#8/#77/#144): the concept-examples surface reuses the
  existing pgvector play-embedding search, degrading to label-linked clips when
  no embedding model is promoted.
* **No player-facing scores** (Non-Goal): the whole surface is coaching-staff
  only via ``Resource.PLAYBOOK``.
* **Optional calibrated tracking** (#127/#128/#129): yard-based rules need a
  calibrated trajectory but degrade instead of requiring it.

See ``docs/playbook-overlays.md`` and ADR 0004.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, NoReturn

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analytics import assignment_scoring as scoring
from app.concept_lexicon import detect_soccer_terms
from app.config import get_settings
from app.database import get_db
from app.governance import Action, Resource, audit_event, require_policy
from app.models import (
    AssignmentScore,
    Clip,
    FieldCalibration,
    Label,
    PlaybookConcept,
    PlayConcept,
    Tracklet,
    User,
    Video,
)
from app.playbook_seed import SEED_CONCEPTS
from app.workload import require_workload_capacity

log = structlog.get_logger(__name__)

_VALID_SIDES = {"offense", "defense"}


async def _require_enabled() -> None:
    """Dark-launch guard — 404 the whole surface when the feature is disabled."""
    if not get_settings().playbook_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Playbook overlays are disabled"
        )


router = APIRouter(
    prefix="/api/v1/playbook",
    tags=["playbook"],
    dependencies=[Depends(_require_enabled)],
)

_ReadUser = Annotated[User, Depends(require_policy(Resource.PLAYBOOK, Action.READ))]
_WriteUser = Annotated[User, Depends(require_policy(Resource.PLAYBOOK, Action.WRITE))]


# ── Schemas ───────────────────────────────────────────────────────────────────


class AssignmentDef(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=64)
    intent: str = ""
    coaching_point: str = ""
    rule: dict[str, Any] = Field(default_factory=lambda: {"type": "manual_only"})
    intended_route: dict[str, Any] | None = None


class ConceptCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    side: str
    structure_kind: str = Field(min_length=1, max_length=32)
    description: str | None = None
    assignments: list[AssignmentDef] = Field(min_length=1)
    coaching_points: list[str] | None = None


class ConceptUpdate(BaseModel):
    structure_kind: str | None = None
    description: str | None = None
    assignments: list[AssignmentDef] | None = None
    coaching_points: list[str] | None = None
    is_active: bool | None = None


class ConceptOut(BaseModel):
    id: uuid.UUID
    name: str
    side: str
    structure_kind: str
    description: str | None
    assignments: list[dict[str, Any]]
    coaching_points: list[str]
    is_active: bool
    created_at: str
    updated_at: str


class ConceptListOut(BaseModel):
    concepts: list[ConceptOut]
    total: int


class LinkCreate(BaseModel):
    concept_id: uuid.UUID
    source: str = "coach"
    confidence: float | None = None
    assignment_player_map: dict[str, Any] | None = None
    validated: bool = False


class LinkOut(BaseModel):
    id: uuid.UUID
    clip_id: uuid.UUID
    concept_id: uuid.UUID
    source: str
    confidence: float | None
    validated: bool
    assignment_player_map: dict[str, Any] | None


class AssignmentOverlayOut(BaseModel):
    assignment_key: str
    role: str
    intent: str
    coaching_point: str
    intended_route: dict[str, Any] | None
    grade: str  # effective grade (coach override wins)
    auto_grade: str
    score: float | None
    confidence: float
    uncertainty: float
    reason_codes: list[str]
    experimental: bool
    status: str
    coach_grade: str | None = None
    coach_comment: str | None = None
    score_id: uuid.UUID | None = None


class PlayExecutionOut(BaseModel):
    play_score: float | None
    graded_count: int
    on_count: int
    off_count: int
    needs_review_count: int
    confidence: float
    experimental: bool


class ConceptOverlay(BaseModel):
    concept_id: uuid.UUID
    name: str
    side: str
    structure_kind: str
    coaching_points: list[str]
    source: str
    validated: bool
    play_execution: PlayExecutionOut
    assignments: list[AssignmentOverlayOut]


class OverlayOut(BaseModel):
    clip_id: uuid.UUID
    overlays: list[ConceptOverlay]
    experimental: bool
    reason: str | None = None


class ScoreOut(BaseModel):
    clip_id: uuid.UUID
    concept_id: uuid.UUID
    play_execution: PlayExecutionOut
    assignments: list[AssignmentOverlayOut]


class OverrideIn(BaseModel):
    coach_grade: str
    coach_comment: str | None = None


class ExampleResult(BaseModel):
    clip_id: uuid.UUID
    source: str  # "concept_link" | "embedding"
    score: float | None = None
    is_experimental: bool
    label_data: dict[str, Any] | None = None


class ExamplesOut(BaseModel):
    concept_id: uuid.UUID
    results: list[ExampleResult]
    approximate: bool
    experimental: bool
    reason: str | None = None


class CorpusScoreOut(BaseModel):
    concept_id: uuid.UUID
    scored_clips: int
    assignment_rows: int


# ── Serialization helpers ───────────────────────────────────────────────────


def _concept_out(c: PlaybookConcept) -> ConceptOut:
    return ConceptOut(
        id=c.id,
        name=c.name,
        side=c.side,
        structure_kind=c.structure_kind,
        description=c.description,
        assignments=list(c.assignments or []),
        coaching_points=list(c.coaching_points or []),
        is_active=c.is_active,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
    )


def _soccer_guard(*texts: str | None) -> None:
    blob = " ".join(t for t in texts if t)
    soccer = detect_soccer_terms(blob)
    if soccer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "soccer_resource_rejected",
                "message": "Football-IQ is American football only; soccer terms are rejected.",
                "terms": soccer[:10],
            },
        )


def _concept_texts(
    name: str | None,
    description: str | None,
    structure_kind: str | None,
    assignments: list[AssignmentDef] | list[dict[str, Any]] | None,
    coaching_points: list[str] | None,
) -> list[str | None]:
    texts: list[str | None] = [name, description, structure_kind, *(coaching_points or [])]
    for assignment in assignments or []:
        if isinstance(assignment, AssignmentDef):
            texts.extend(
                [
                    assignment.key,
                    assignment.role,
                    assignment.intent,
                    assignment.coaching_point,
                ]
            )
            continue
        texts.extend(
            [
                assignment.get("key"),
                assignment.get("role"),
                assignment.get("intent"),
                assignment.get("coaching_point"),
            ]
        )
    return texts


def _rule_error(message: str) -> NoReturn:
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


def _require_rule_number(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        _rule_error(f"Rule {field_name} must be numeric")


def _validate_rule(rule: dict[str, Any]) -> None:
    if not isinstance(rule, dict):
        _rule_error("Assignment rule must be an object")
    rule_type = str(rule.get("type", "manual_only"))
    params = rule.get("params") or {}
    if not isinstance(params, dict):
        _rule_error("Rule params must be an object")
    if "pass_score" in rule:
        pass_score = _require_rule_number(rule["pass_score"], "pass_score")
        if not 0.0 <= pass_score <= 1.0:
            _rule_error("Rule pass_score must be between 0 and 1")
    if rule_type == "release_direction":
        axis = str(params.get("axis", "y"))
        if axis not in {"x", "y"}:
            _rule_error("release_direction axis must be 'x' or 'y'")
        if _require_rule_number(params.get("min_delta", 0.05), "release_direction.min_delta") < 0:
            _rule_error("release_direction min_delta must be non-negative")
        _require_rule_number(params.get("sign", 1), "release_direction.sign")
        _require_rule_number(params.get("window", 0.4), "release_direction.window")
    elif rule_type == "zone_landmark":
        region = params.get("region")
        if not isinstance(region, dict):
            _rule_error("zone_landmark region must be an object with x/y bounds")
        for axis in ("x", "y"):
            bounds = region.get(axis)
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                _rule_error(f"zone_landmark region.{axis} must contain exactly two bounds")
            lower = _require_rule_number(bounds[0], f"zone_landmark.region.{axis}[0]")
            upper = _require_rule_number(bounds[1], f"zone_landmark.region.{axis}[1]")
            if lower > upper:
                _rule_error(f"zone_landmark region.{axis} lower bound must not exceed upper bound")
        if _require_rule_number(params.get("tolerance", 0.25), "zone_landmark.tolerance") <= 0:
            _rule_error("zone_landmark tolerance must be positive")
    elif rule_type == "depth_threshold":
        if (
            _require_rule_number(
                params.get("min_depth_yards", 12.0),
                "depth_threshold.min_depth_yards",
            )
            <= 0
        ):
            _rule_error("depth_threshold min_depth_yards must be positive")
    elif rule_type == "presence":
        try:
            min_points = int(params.get("min_points", 3))
        except (TypeError, ValueError):
            _rule_error("presence min_points must be an integer")
        if min_points <= 0:
            _rule_error("presence min_points must be positive")


def _validate_assignments(assignments: list[AssignmentDef]) -> None:
    keys = [a.key for a in assignments]
    if len(keys) != len(set(keys)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assignment keys must be unique within a concept",
        )
    for assignment in assignments:
        _validate_rule(assignment.rule)


def _concept_definition(c: PlaybookConcept) -> scoring.ConceptDefinition:
    defs: list[scoring.AssignmentDefinition] = []
    for a in c.assignments or []:
        defs.append(
            scoring.AssignmentDefinition(
                key=str(a.get("key")),
                role=str(a.get("role", "")),
                intent=str(a.get("intent", "")),
                coaching_point=str(a.get("coaching_point", "")),
                rule=dict(a.get("rule") or {"type": "manual_only"}),
                intended_route=a.get("intended_route"),
            )
        )
    return scoring.ConceptDefinition(
        name=c.name,
        side=c.side,
        structure_kind=c.structure_kind,
        assignments=defs,
        coaching_points=list(c.coaching_points or []),
    )


def _trajectory(tracklet: Tracklet, video: Video | None) -> list[scoring.TrajectoryPoint]:
    """Build a normalized + field-coordinate trajectory from a tracklet's points.

    Normalized frame coords come from the bbox centre and the parent video's
    pixel dimensions; field coords are passed straight through (``None`` unless
    calibration produced them). Without video dimensions, normalized coords are
    omitted so frame-geometry rules degrade rather than guess.
    """
    width = float(video.width) if video and video.width else None
    height = float(video.height) if video and video.height else None
    points: list[scoring.TrajectoryPoint] = []
    for tp in sorted(tracklet.track_points, key=lambda p: p.frame_number):
        norm_x = norm_y = None
        if tp.bbox and width and height and len(tp.bbox) >= 4:
            cx = (tp.bbox[0] + tp.bbox[2]) / 2.0
            cy = (tp.bbox[1] + tp.bbox[3]) / 2.0
            norm_x = scoring._clamp01(cx / width)
            norm_y = scoring._clamp01(cy / height)
        if norm_x is None and norm_y is None and tp.field_x is None and tp.field_y is None:
            continue
        points.append(
            scoring.TrajectoryPoint(
                norm_x=norm_x, norm_y=norm_y, field_x=tp.field_x, field_y=tp.field_y
            )
        )
    return points


def _map_entry(raw: Any) -> dict[str, Any]:
    """Normalize an assignment_player_map entry into a dict."""
    if isinstance(raw, str):
        return {"tracklet_id": raw}
    if isinstance(raw, dict):
        return raw
    return {}


def _build_substrates(
    concept: PlaybookConcept,
    link: PlayConcept,
    tracklets_by_id: dict[str, Tracklet],
    video: Video | None,
    calibration_confidence: float | None,
) -> dict[str, scoring.AssignmentSubstrate]:
    mapping = link.assignment_player_map or {}
    out: dict[str, scoring.AssignmentSubstrate] = {}
    for a in concept.assignments or []:
        key = str(a.get("key"))
        entry = _map_entry(mapping.get(key))
        tracklet_id = entry.get("tracklet_id")
        if not tracklet_id:
            out[key] = scoring.AssignmentSubstrate(
                assignment_key=key,
                resolution="no_mapping",
                concept_source=link.source,
                concept_validated=link.validated,
            )
            continue
        tracklet = tracklets_by_id.get(str(tracklet_id))
        if tracklet is None:
            out[key] = scoring.AssignmentSubstrate(
                assignment_key=key,
                resolution="tracklet_not_found",
                tracklet_id=str(tracklet_id),
                concept_source=link.source,
                concept_validated=link.validated,
            )
            continue
        traj = _trajectory(tracklet, video)
        # Identity: explicit map override wins; otherwise derive conservatively
        # from the tracklet (a bare player_id is only "probable", never face ID).
        identity_state = entry.get("identity_state")
        identity_conf = entry.get("identity_confidence")
        if identity_state is None:
            if tracklet.player_id is not None:
                identity_state = "probable"
                identity_conf = (
                    identity_conf if identity_conf is not None else tracklet.track_confidence
                )
            else:
                identity_state = "unknown"
        out[key] = scoring.AssignmentSubstrate(
            assignment_key=key,
            resolution="ok",
            tracklet_id=str(tracklet_id),
            identity_state=identity_state,
            identity_confidence=identity_conf,
            track_confidence=tracklet.track_confidence,
            calibration_confidence=calibration_confidence,
            has_calibrated_trajectory=any(p.field_y is not None for p in traj),
            trajectory=traj,
            concept_source=link.source,
            concept_validated=link.validated,
        )
    return out


async def _calibration_confidence(db: AsyncSession, clip: Clip) -> float | None:
    if clip.calibration_version_id is None:
        return None
    row = await db.execute(
        select(FieldCalibration.confidence).where(
            FieldCalibration.id == clip.calibration_version_id
        )
    )
    return row.scalar_one_or_none()


async def _tracklets_for_clip(db: AsyncSession, clip_id: uuid.UUID) -> dict[str, Tracklet]:
    rows = await db.execute(
        select(Tracklet)
        .options(selectinload(Tracklet.track_points))
        .where(Tracklet.clip_id == clip_id)
    )
    return {str(t.id): t for t in rows.scalars().all()}


def _assignment_overlay(
    defn: scoring.AssignmentDefinition,
    result: scoring.AssignmentScoreResult,
    row: AssignmentScore | None,
) -> AssignmentOverlayOut:
    coach_grade = row.coach_grade if row else None
    status_val = row.status if row else "auto"
    return AssignmentOverlayOut(
        assignment_key=defn.key,
        role=defn.role,
        intent=defn.intent,
        coaching_point=defn.coaching_point,
        intended_route=defn.intended_route,
        grade=coach_grade or result.grade,
        auto_grade=result.grade,
        score=result.score,
        confidence=result.confidence,
        uncertainty=result.uncertainty,
        reason_codes=result.reason_codes,
        experimental=result.experimental,
        status=status_val,
        coach_grade=coach_grade,
        coach_comment=row.coach_comment if row else None,
        score_id=row.id if row else None,
    )


def _play_execution_out(play: scoring.PlayExecutionResult) -> PlayExecutionOut:
    return PlayExecutionOut(
        play_score=play.play_score,
        graded_count=play.graded_count,
        on_count=play.on_count,
        off_count=play.off_count,
        needs_review_count=play.needs_review_count,
        confidence=play.confidence,
        experimental=play.experimental,
    )


def _effective_assignment_result(
    result: scoring.AssignmentScoreResult,
    row: AssignmentScore | None,
) -> scoring.AssignmentScoreResult:
    if row is None or row.status != "coach_overridden" or row.coach_grade is None:
        return result
    score = None
    if row.coach_grade == scoring.GRADE_ON:
        score = 1.0
    elif row.coach_grade == scoring.GRADE_OFF:
        score = 0.0
    return scoring.AssignmentScoreResult(
        assignment_key=result.assignment_key,
        grade=row.coach_grade,
        score=score,
        confidence=1.0 if row.coach_grade != scoring.GRADE_REVIEW else 0.0,
        uncertainty=0.0 if row.coach_grade != scoring.GRADE_REVIEW else 1.0,
        reason_codes=result.reason_codes,
        experimental=False,
        detail=result.detail,
    )


def _effective_play_execution(
    definitions: list[scoring.AssignmentDefinition],
    results_by_key: dict[str, scoring.AssignmentScoreResult],
    persisted: dict[str, AssignmentScore],
) -> PlayExecutionOut:
    effective_results = [
        _effective_assignment_result(results_by_key[d.key], persisted.get(d.key))
        for d in definitions
    ]
    return _play_execution_out(scoring.summarize_play(effective_results))


async def _score_link(
    db: AsyncSession, clip: Clip, link: PlayConcept, concept: PlaybookConcept
) -> tuple[scoring.PlayExecutionResult, list[scoring.AssignmentDefinition]]:
    video = None
    if clip.video_id is not None:
        video = (
            await db.execute(select(Video).where(Video.id == clip.video_id))
        ).scalar_one_or_none()
    calib = await _calibration_confidence(db, clip)
    tracklets = await _tracklets_for_clip(db, clip.id)
    substrates = _build_substrates(concept, link, tracklets, video, calib)
    definition = _concept_definition(concept)
    play = scoring.score_play(
        definition,
        substrates,
        identity_confidence_threshold=get_settings().playbook_identity_confidence_threshold,
    )
    return play, definition.assignments


# ── Concept CRUD ────────────────────────────────────────────────────────────


@router.get("/concepts", response_model=ConceptListOut)
async def list_concepts(
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: _ReadUser,
    side: str | None = Query(default=None),
    active_only: bool = Query(default=True),
) -> ConceptListOut:
    """List playbook concepts (coaching staff only)."""
    q = select(PlaybookConcept).order_by(PlaybookConcept.name)
    if side is not None:
        q = q.where(PlaybookConcept.side == side)
    if active_only:
        q = q.where(PlaybookConcept.is_active.is_(True))
    rows = list((await db.execute(q)).scalars().all())
    return ConceptListOut(concepts=[_concept_out(c) for c in rows], total=len(rows))


@router.post("/concepts", response_model=ConceptOut, status_code=status.HTTP_201_CREATED)
async def create_concept(
    body: ConceptCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _WriteUser,
) -> ConceptOut:
    """Create a coach-authored concept."""
    if body.side not in _VALID_SIDES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"side must be one of {sorted(_VALID_SIDES)}",
        )
    _validate_assignments(body.assignments)
    _soccer_guard(
        *_concept_texts(
            body.name,
            body.description,
            body.structure_kind,
            body.assignments,
            body.coaching_points,
        )
    )

    existing = (
        await db.execute(select(PlaybookConcept).where(PlaybookConcept.name == body.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A concept with this name already exists"
        )

    concept = PlaybookConcept(
        id=uuid.uuid4(),
        name=body.name,
        side=body.side,
        structure_kind=body.structure_kind,
        description=body.description,
        assignments=[a.model_dump() for a in body.assignments],
        coaching_points=body.coaching_points,
        created_by=user.id,
    )
    db.add(concept)
    await db.flush()
    audit_event(
        "audit.playbook.concept_upserted",
        actor_id=user.id,
        actor_role=user.role.value,
        concept_id=concept.id,
    )
    return _concept_out(concept)


@router.post("/concepts/seed", response_model=ConceptListOut)
async def seed_concepts(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _WriteUser,
) -> ConceptListOut:
    """Install the starter offense + defense concepts (idempotent)."""
    out: list[PlaybookConcept] = []
    for spec in SEED_CONCEPTS:
        existing = (
            await db.execute(select(PlaybookConcept).where(PlaybookConcept.name == spec["name"]))
        ).scalar_one_or_none()
        if existing is not None:
            out.append(existing)
            continue
        concept = PlaybookConcept(
            id=uuid.uuid4(),
            name=spec["name"],
            side=spec["side"],
            structure_kind=spec["structure_kind"],
            description=spec.get("description"),
            assignments=spec["assignments"],
            coaching_points=spec.get("coaching_points"),
            created_by=user.id,
        )
        db.add(concept)
        await db.flush()
        audit_event(
            "audit.playbook.concept_upserted",
            actor_id=user.id,
            actor_role=user.role.value,
            concept_id=concept.id,
            source="seed",
        )
        out.append(concept)
    return ConceptListOut(concepts=[_concept_out(c) for c in out], total=len(out))


@router.get("/concepts/{concept_id}", response_model=ConceptOut)
async def get_concept(
    concept_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: _ReadUser,
) -> ConceptOut:
    concept = (
        await db.execute(select(PlaybookConcept).where(PlaybookConcept.id == concept_id))
    ).scalar_one_or_none()
    if concept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")
    return _concept_out(concept)


@router.patch("/concepts/{concept_id}", response_model=ConceptOut)
async def update_concept(
    concept_id: uuid.UUID,
    body: ConceptUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _WriteUser,
) -> ConceptOut:
    """Edit a concept (coach-curated)."""
    concept = (
        await db.execute(select(PlaybookConcept).where(PlaybookConcept.id == concept_id))
    ).scalar_one_or_none()
    if concept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")

    assignments = (
        body.assignments if body.assignments is not None else list(concept.assignments or [])
    )
    coaching_points = (
        body.coaching_points
        if body.coaching_points is not None
        else list(concept.coaching_points or [])
    )
    structure_kind = (
        body.structure_kind if body.structure_kind is not None else concept.structure_kind
    )
    description = body.description if body.description is not None else concept.description

    if body.assignments is not None:
        _validate_assignments(body.assignments)
    _soccer_guard(
        *_concept_texts(concept.name, description, structure_kind, assignments, coaching_points)
    )

    if body.assignments is not None:
        concept.assignments = [a.model_dump() for a in body.assignments]
    if body.structure_kind is not None:
        concept.structure_kind = structure_kind
    if body.description is not None:
        concept.description = description
    if body.coaching_points is not None:
        concept.coaching_points = coaching_points
    if body.is_active is not None:
        concept.is_active = body.is_active

    await db.flush()
    audit_event(
        "audit.playbook.concept_upserted",
        actor_id=user.id,
        actor_role=user.role.value,
        concept_id=concept.id,
    )
    return _concept_out(concept)


# ── Concept ↔ clip linking ──────────────────────────────────────────────────


@router.post(
    "/clips/{clip_id}/concept", response_model=LinkOut, status_code=status.HTTP_201_CREATED
)
async def link_concept(
    clip_id: uuid.UUID,
    body: LinkCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _WriteUser,
) -> LinkOut:
    """Record that ``concept_id`` was the call on this clip (upsert)."""
    clip = (await db.execute(select(Clip).where(Clip.id == clip_id))).scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    concept = (
        await db.execute(select(PlaybookConcept).where(PlaybookConcept.id == body.concept_id))
    ).scalar_one_or_none()
    if concept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")

    link = (
        await db.execute(
            select(PlayConcept).where(
                PlayConcept.clip_id == clip_id, PlayConcept.concept_id == body.concept_id
            )
        )
    ).scalar_one_or_none()
    if link is None:
        link = PlayConcept(id=uuid.uuid4(), clip_id=clip_id, concept_id=body.concept_id)
        db.add(link)
    link.source = body.source
    link.confidence = body.confidence
    link.assignment_player_map = body.assignment_player_map
    link.validated = body.validated
    link.created_by = user.id
    await db.flush()
    audit_event(
        "audit.playbook.concept_linked",
        actor_id=user.id,
        actor_role=user.role.value,
        clip_id=clip_id,
        concept_id=body.concept_id,
        source=body.source,
    )
    return LinkOut(
        id=link.id,
        clip_id=link.clip_id,
        concept_id=link.concept_id,
        source=link.source,
        confidence=link.confidence,
        validated=link.validated,
        assignment_player_map=link.assignment_player_map,
    )


# ── Overlay + scoring ───────────────────────────────────────────────────────


async def _links_with_concepts(
    db: AsyncSession, clip_id: uuid.UUID
) -> list[tuple[PlayConcept, PlaybookConcept]]:
    rows = await db.execute(
        select(PlayConcept)
        .options(selectinload(PlayConcept.concept))
        .where(PlayConcept.clip_id == clip_id)
        .order_by(PlayConcept.created_at.desc())
    )
    return [(link, link.concept) for link in rows.scalars().all() if link.concept is not None]


async def _persisted_scores(
    db: AsyncSession, clip_id: uuid.UUID, concept_id: uuid.UUID
) -> dict[str, AssignmentScore]:
    rows = await db.execute(
        select(AssignmentScore).where(
            AssignmentScore.clip_id == clip_id, AssignmentScore.concept_id == concept_id
        )
    )
    return {r.assignment_key: r for r in rows.scalars().all()}


@router.get("/clips/{clip_id}/overlay", response_model=OverlayOut)
async def get_overlay(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: _ReadUser,
) -> OverlayOut:
    """Film Room overlay for a clip: intended routes, coaching points, and live
    assignment grades (with any coach overrides merged in).

    Degrades gracefully: no linked concept → empty overlay with a reason; missing
    tracklets/identity → ``needs_review`` per assignment, never a hard error.
    """
    clip = (await db.execute(select(Clip).where(Clip.id == clip_id))).scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    links = await _links_with_concepts(db, clip_id)
    if not links:
        return OverlayOut(
            clip_id=clip_id,
            overlays=[],
            experimental=False,
            reason="No playbook concept linked to this clip yet.",
        )

    overlays: list[ConceptOverlay] = []
    any_experimental = False
    for link, concept in links:
        play, definitions = await _score_link(db, clip, link, concept)
        persisted = await _persisted_scores(db, clip_id, concept.id)
        result_by_key = {r.assignment_key: r for r in play.assignments}
        assignment_outs = [
            _assignment_overlay(d, result_by_key[d.key], persisted.get(d.key)) for d in definitions
        ]
        play_execution = _effective_play_execution(definitions, result_by_key, persisted)
        any_experimental = any_experimental or play_execution.experimental
        overlays.append(
            ConceptOverlay(
                concept_id=concept.id,
                name=concept.name,
                side=concept.side,
                structure_kind=concept.structure_kind,
                coaching_points=list(concept.coaching_points or []),
                source=link.source,
                validated=link.validated,
                play_execution=play_execution,
                assignments=assignment_outs,
            )
        )
    return OverlayOut(clip_id=clip_id, overlays=overlays, experimental=any_experimental)


async def _persist_scores(
    db: AsyncSession,
    clip_id: uuid.UUID,
    concept_id: uuid.UUID,
    play: scoring.PlayExecutionResult,
) -> dict[str, AssignmentScore]:
    """Upsert AssignmentScore rows from a freshly computed play result.

    A coach override (``status == "coach_overridden"``) is preserved — only the
    recomputed auto fields are refreshed underneath it.
    """
    existing = await _persisted_scores(db, clip_id, concept_id)
    for r in play.assignments:
        row = existing.get(r.assignment_key)
        if row is None:
            row = AssignmentScore(
                id=uuid.uuid4(),
                clip_id=clip_id,
                concept_id=concept_id,
                assignment_key=r.assignment_key,
            )
            db.add(row)
            existing[r.assignment_key] = row
        row.grade = r.grade
        row.score = r.score
        row.confidence = r.confidence
        row.uncertainty = r.uncertainty
        row.reason_codes = r.reason_codes
        row.inputs = r.detail
        row.experimental = r.experimental
        if row.status != "coach_overridden":
            row.status = "auto"
    await db.flush()
    return existing


@router.post("/clips/{clip_id}/score", response_model=list[ScoreOut])
async def score_clip(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _WriteUser,
) -> list[ScoreOut]:
    """Compute and persist assignment execution scores for the clip's concepts."""
    clip = (await db.execute(select(Clip).where(Clip.id == clip_id))).scalar_one_or_none()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    links = await _links_with_concepts(db, clip_id)
    if not links:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No playbook concept linked to this clip",
        )

    out: list[ScoreOut] = []
    for link, concept in links:
        play, definitions = await _score_link(db, clip, link, concept)
        persisted = await _persist_scores(db, clip_id, concept.id, play)
        result_by_key = {r.assignment_key: r for r in play.assignments}
        out.append(
            ScoreOut(
                clip_id=clip_id,
                concept_id=concept.id,
                play_execution=_effective_play_execution(definitions, result_by_key, persisted),
                assignments=[
                    _assignment_overlay(d, result_by_key[d.key], persisted.get(d.key))
                    for d in definitions
                ],
            )
        )
        audit_event(
            "audit.playbook.scored",
            actor_id=user.id,
            actor_role=user.role.value,
            clip_id=clip_id,
            concept_id=concept.id,
            count=len(play.assignments),
        )
    return out


@router.patch("/scores/{score_id}", response_model=AssignmentOverlayOut)
async def override_score(
    score_id: uuid.UUID,
    body: OverrideIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _WriteUser,
) -> AssignmentOverlayOut:
    """Coach override of an assignment grade.

    The override is authoritative on the row and is *also* written as an
    ``assignment_execution`` training label so corrections feed the flywheel.
    """
    valid_grades = {scoring.GRADE_ON, scoring.GRADE_OFF, scoring.GRADE_REVIEW}
    if body.coach_grade not in valid_grades:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"coach_grade must be one of {sorted(valid_grades)}",
        )
    row = (
        await db.execute(select(AssignmentScore).where(AssignmentScore.id == score_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Score not found")

    previous_auto = row.grade
    row.coach_grade = body.coach_grade
    row.coach_comment = body.coach_comment
    row.status = "coach_overridden"
    row.overridden_by = user.id
    row.overridden_at = datetime.now(UTC)

    db.add(
        Label(
            id=uuid.uuid4(),
            clip_id=row.clip_id,
            label_type="assignment_execution",
            label_value={
                "concept_id": str(row.concept_id),
                "assignment_key": row.assignment_key,
                "coach_grade": body.coach_grade,
                "previous_auto_grade": previous_auto,
            },
            annotated_by=user.id,
            source="human",
        )
    )
    await db.flush()
    audit_event(
        "audit.playbook.score_overridden",
        actor_id=user.id,
        actor_role=user.role.value,
        clip_id=row.clip_id,
        concept_id=row.concept_id,
        score_id=row.id,
        assignment_key=row.assignment_key,
        grade=body.coach_grade,
    )

    concept = (
        await db.execute(select(PlaybookConcept).where(PlaybookConcept.id == row.concept_id))
    ).scalar_one()
    defn = next(
        (d for d in _concept_definition(concept).assignments if d.key == row.assignment_key),
        scoring.AssignmentDefinition(
            key=row.assignment_key, role="", intent="", coaching_point="", rule={}
        ),
    )
    # Re-wrap the persisted auto fields into a result for the response shape.
    auto_result = scoring.AssignmentScoreResult(
        assignment_key=row.assignment_key,
        grade=row.grade,
        score=row.score,
        confidence=row.confidence,
        uncertainty=row.uncertainty,
        reason_codes=list(row.reason_codes or []),
        experimental=row.experimental,
        detail=dict(row.inputs or {}),
    )
    return _assignment_overlay(defn, auto_result, row)


# ── Concept examples (embeddings with graceful fallback) ─────────────────────


@router.get("/concepts/{concept_id}/examples", response_model=ExamplesOut)
async def concept_examples(
    concept_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: _ReadUser,
    k: int = Query(default=12, ge=1, le=50),
) -> ExamplesOut:
    """Surface example reps for a concept.

    Primary signal: clips already linked to the concept (real, not experimental).
    Optional expansion: the existing pgvector play-embedding search seeds from
    those clips to surface visually similar reps (always flagged experimental).
    Works fully without any embedding model.
    """
    concept = (
        await db.execute(select(PlaybookConcept).where(PlaybookConcept.id == concept_id))
    ).scalar_one_or_none()
    if concept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")

    linked = await db.execute(
        select(PlayConcept.clip_id)
        .where(PlayConcept.concept_id == concept_id)
        .order_by(PlayConcept.created_at.desc())
        .limit(k)
    )
    seed_clip_ids = [cid for (cid,) in linked.all()]
    results: list[ExampleResult] = []
    seen: set[uuid.UUID] = set()
    for cid in seed_clip_ids:
        seen.add(cid)
        results.append(ExampleResult(clip_id=cid, source="concept_link", is_experimental=False))

    experimental = False
    settings = get_settings()
    if settings.concept_search_embedding_expansion and seed_clip_ids and len(results) < k:
        try:
            from app.routers.concept_search import _centroid, _load_seed_vectors
            from app.routers.search import (
                _resolve_production_model_version,
                _run_vector_search,
            )

            mv = await _resolve_production_model_version(db)
            if mv is not None:
                vectors = await _load_seed_vectors(db, seed_clip_ids[:50], mv.id)
                centroid = _centroid(vectors)
                if centroid is not None:
                    expansion = await _run_vector_search(
                        db,
                        anchor_vector=centroid,
                        k=k,
                        chunk_kind="play",
                        model_version_id=mv.id,
                        candidate_clip_ids=None,
                        include_experimental=True,
                        exclude_clip_ids=list(seen),
                    )
                    for r in expansion:
                        if r.clip_id in seen:
                            continue
                        seen.add(r.clip_id)
                        experimental = True
                        sim = max(-1.0, min(1.0, r.score))
                        results.append(
                            ExampleResult(
                                clip_id=r.clip_id,
                                source="embedding",
                                score=round(sim, 3),
                                is_experimental=True,
                                label_data=r.label_data,
                            )
                        )
        except Exception as exc:  # pragma: no cover - expansion is best-effort
            log.warning("playbook_examples_expansion_failed", error=str(exc))

    reason = None if results else "No linked example reps for this concept yet."
    return ExamplesOut(
        concept_id=concept_id,
        results=results[:k],
        approximate=experimental,
        experimental=experimental,
        reason=reason,
    )


# ── Heavy: corpus rescore (workload-gated) ──────────────────────────────────


@router.post("/concepts/{concept_id}/score-corpus", response_model=CorpusScoreOut)
async def score_corpus(
    concept_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _WriteUser,
    _capacity: Annotated[Any, Depends(require_workload_capacity("playbook.score_corpus"))],
) -> CorpusScoreOut:
    """Recompute and persist assignment scores for every clip linked to a concept.

    Heavy enough to be workload-gated: it scans every linked clip's tracklets.
    Capped by ``PLAYBOOK_SCORE_CORPUS_MAX_PLAYS``.
    """
    concept = (
        await db.execute(select(PlaybookConcept).where(PlaybookConcept.id == concept_id))
    ).scalar_one_or_none()
    if concept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Concept not found")

    cap = get_settings().playbook_score_corpus_max_plays
    links = list(
        (
            await db.execute(
                select(PlayConcept)
                .where(PlayConcept.concept_id == concept_id)
                .order_by(PlayConcept.created_at.desc())
                .limit(cap)
            )
        )
        .scalars()
        .all()
    )

    scored_clips = 0
    assignment_rows = 0
    for link in links:
        clip = (await db.execute(select(Clip).where(Clip.id == link.clip_id))).scalar_one_or_none()
        if clip is None:
            continue
        play, _defs = await _score_link(db, clip, link, concept)
        rows = await _persist_scores(db, clip.id, concept_id, play)
        scored_clips += 1
        assignment_rows += len(rows)

    audit_event(
        "audit.playbook.corpus_scored",
        actor_id=user.id,
        actor_role=user.role.value,
        concept_id=concept_id,
        count=scored_clips,
    )
    return CorpusScoreOut(
        concept_id=concept_id, scored_clips=scored_clips, assignment_rows=assignment_rows
    )
