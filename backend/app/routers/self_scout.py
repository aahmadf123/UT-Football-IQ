"""Self-scout exposure dashboard — Phase 2.

Provides tendency analysis endpoints for the coaching staff to identify
pre-snap tells and formation/motion/field-zone/personnel tendencies.
"""

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import tendency_break as tb
from app.cfbd.models import CFBDPlay
from app.database import get_db
from app.deps import get_current_user, require_coach_or_above
from app.models import (
    Alert,
    AlertSeverity,
    AlertType,
    Clip,
    Label,
    PlayView,
    SessionKind,
    User,
    UserRole,
    Video,
)
from app.routers.alerts_sse import publish_alert
from app.services.metric_policy import canonical_play_eligible
from app.workload import require_workload_capacity

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/self-scout", tags=["self-scout"])

# Tendency-break alerts are an offense-wide scouting concern, not tied to a
# single position group; they are surfaced via the self-scout endpoints below
# (staff-only) rather than the position-scoped alerts list.
TENDENCY_POSITION_GROUP = "OFFENSE"
SEASON_SESSION_ID = "season"

TENDENCY_ALERT_THRESHOLD = 0.70
PRE_SNAP_TELL_THRESHOLD = 0.80
PRE_SNAP_TELL_HIGH_SEVERITY_THRESHOLD = 0.90
LOW_SAMPLE_THRESHOLD = 10
EVIDENCE_CLIP_LIMIT = 10
DISTANCE_BUCKET_ORDER = {"short": 0, "medium": 1, "long": 2}
MOTION_STATE_LABELS = {
    "with_motion": "with motion",
    "without_motion": "without motion",
}


# ── Schemas ───────────────────────────────────────────────────────────────────


class TendencyEntry(BaseModel):
    grouping_key: str
    total_plays: int
    run_count: int
    pass_count: int
    run_rate: float
    pass_rate: float
    evidence_clip_ids: list[str]
    low_sample: bool


class MotionTendency(BaseModel):
    total: int
    run_count: int
    pass_count: int
    run_rate: float
    pass_rate: float


class MotionTendencyResponse(BaseModel):
    with_motion: MotionTendency
    without_motion: MotionTendency


class DownDistanceTendency(BaseModel):
    down: int
    distance_bucket: str
    total_plays: int
    run_count: int
    pass_count: int
    run_rate: float
    pass_rate: float
    evidence_clip_ids: list[str]
    low_sample: bool


class ConceptFamilyEntry(BaseModel):
    formation: str
    concept_family: str
    total_plays: int
    rate: float
    evidence_clip_ids: list[str]
    low_sample: bool


class TendencyAlert(BaseModel):
    alert_type: str
    message: str
    severity: str
    grouping_key: str
    run_rate: float
    pass_rate: float


class ExposureAlert(BaseModel):
    grouping_key: str
    formation: str
    motion_state: str
    total_plays: int
    lean: str
    severity: str
    run_rate: float
    pass_rate: float
    evidence_clip_ids: list[str]
    low_sample: bool
    message: str


class SelfScoutResponse(BaseModel):
    formation_tendencies: list[TendencyEntry]
    motion_tendencies: MotionTendencyResponse
    field_zone_tendencies: list[TendencyEntry]
    personnel_tendencies: list[TendencyEntry]
    down_distance_tendencies: list[DownDistanceTendency]
    formation_concept_families: dict[str, list[ConceptFamilyEntry]]
    pre_snap_tells: list[ExposureAlert]
    alerts: list[TendencyAlert]
    clip_count: int


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/tendencies", response_model=SelfScoutResponse)
async def get_tendencies(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
    video_id: uuid.UUID | None = Query(default=None),
    session_id: uuid.UUID | None = Query(default=None),
    recorded_after: datetime | None = Query(default=None),
    recorded_before: datetime | None = Query(default=None),
    session_kind: SessionKind | None = Query(default=None),
    library_role: str | None = Query(default=None, pattern="^(toledo|opponent)$"),
    limit: int = Query(default=500, ge=1, le=2000),
) -> SelfScoutResponse:
    """Compute and return self-scout tendency analysis."""
    log.info(
        "self_scout_tendencies_requested",
        video_id=str(video_id) if video_id is not None else None,
        limit=limit,
    )
    return await _run_tendency_engine(
        db=db,
        video_id=video_id,
        limit=limit,
        session_id=session_id,
        recorded_after=recorded_after,
        recorded_before=recorded_before,
        session_kind=session_kind,
        library_role=library_role,
    )


@router.get("/opponent-tendencies", response_model=SelfScoutResponse)
async def get_opponent_tendencies(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
    opponent_video_id: uuid.UUID = Query(...),
    limit: int = Query(default=500, ge=1, le=2000),
) -> SelfScoutResponse:
    """Compute and return opponent tendency analysis for a single video."""
    log.info(
        "opponent_tendencies_requested",
        opponent_video_id=str(opponent_video_id),
        limit=limit,
    )
    return await _run_tendency_engine(db=db, video_id=opponent_video_id, limit=limit)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _run_tendency_engine(
    db: AsyncSession,
    video_id: uuid.UUID | None,
    limit: int,
    session_id: uuid.UUID | None = None,
    recorded_after: datetime | None = None,
    recorded_before: datetime | None = None,
    session_kind: SessionKind | None = None,
    library_role: str | None = None,
) -> SelfScoutResponse:
    play_key = func.coalesce(PlayView.play_id, Clip.id)
    q = (
        select(Clip)
        .join(Video, Video.id == Clip.video_id)
        .outerjoin(PlayView, PlayView.clip_id == Clip.id)
        .where(canonical_play_eligible())
        .distinct(play_key)
        .order_by(play_key, Clip.is_reviewed.desc(), Clip.created_at)
        .limit(limit)
    )
    if session_id is not None:
        q = q.where(Video.practice_session_id == session_id)
    if recorded_after is not None:
        q = q.where(Video.recorded_at >= recorded_after)
    if recorded_before is not None:
        q = q.where(Video.recorded_at <= recorded_before)
    if session_kind is not None:
        q = q.where(Video.session_kind == session_kind)
    if library_role is not None:
        q = q.where(Video.library_role == library_role)
    if video_id is not None:
        q = q.where(Clip.video_id == video_id)
    clip_result = await db.execute(q)
    clips = list(clip_result.scalars().all())

    clip_ids = [c.id for c in clips]
    if not clip_ids:
        return _empty_self_scout_response()

    # Human tags are usable immediately. Unvalidated model/heuristic proposals
    # stay review candidates and never silently enter a trusted tendency.
    labels_result = await db.execute(
        select(Label)
        .where(
            Label.clip_id.in_(clip_ids),
            (Label.source == "human")
            | (
                (Label.source == "model")
                & Label.label_value["provenance"]["validated"].as_boolean().is_(True)
            ),
        )
        .order_by(Label.created_at.desc())
    )
    all_labels = list(labels_result.scalars().all())

    labels_by_clip: dict[uuid.UUID, list[Label]] = defaultdict(list)
    for lbl in all_labels:
        if lbl.clip_id is not None:
            labels_by_clip[lbl.clip_id].append(lbl)

    formation_plays: dict[str, Counter[str]] = defaultdict(Counter)
    formation_evidence: dict[str, list[str]] = defaultdict(list)
    motion_plays: Counter[str] = Counter()
    no_motion_plays: Counter[str] = Counter()
    field_zone_plays: dict[str, Counter[str]] = defaultdict(Counter)
    field_zone_evidence: dict[str, list[str]] = defaultdict(list)
    personnel_plays: dict[str, Counter[str]] = defaultdict(Counter)
    personnel_evidence: dict[str, list[str]] = defaultdict(list)
    down_distance_plays: dict[tuple[int, str], Counter[str]] = defaultdict(Counter)
    down_distance_evidence: dict[tuple[int, str], list[str]] = defaultdict(list)
    concept_family_plays: dict[str, Counter[str]] = defaultdict(Counter)
    concept_family_evidence: dict[tuple[str, str], list[str]] = defaultdict(list)
    exposure_plays: dict[str, Counter[str]] = defaultdict(Counter)
    exposure_evidence: dict[str, list[str]] = defaultdict(list)
    exposure_meta: dict[str, tuple[str, str]] = {}

    for clip in clips:
        clip_labels = labels_by_clip.get(clip.id, [])
        play_type = _get_play_type(clip_labels)
        if play_type is None:
            continue

        clip_id = str(clip.id)
        formation = _get_formation(clip_labels)
        has_motion = _get_motion_detected(clip_labels)
        field_zone = clip.field_zone or "unknown"
        personnel = clip.personnel_grouping or "unknown"
        down_distance_key = _get_down_distance_key(clip)
        concept_family = _get_concept_family(clip_labels)

        if formation:
            formation_plays[formation][play_type] += 1
            formation_evidence[formation].append(clip_id)

            motion_state = "with_motion" if has_motion else "without_motion"
            exposure_key = f"{formation}|{motion_state}"
            exposure_meta[exposure_key] = (formation, motion_state)
            exposure_plays[exposure_key][play_type] += 1
            exposure_evidence[exposure_key].append(clip_id)

            if concept_family is not None:
                concept_family_plays[formation][concept_family] += 1
                concept_family_evidence[(formation, concept_family)].append(clip_id)

        if has_motion:
            motion_plays[play_type] += 1
        else:
            no_motion_plays[play_type] += 1

        field_zone_plays[field_zone][play_type] += 1
        field_zone_evidence[field_zone].append(clip_id)

        personnel_plays[personnel][play_type] += 1
        personnel_evidence[personnel].append(clip_id)

        if down_distance_key is not None:
            down_distance_plays[down_distance_key][play_type] += 1
            down_distance_evidence[down_distance_key].append(clip_id)

    formation_tendencies = _build_tendencies(formation_plays, formation_evidence, min_plays=3)
    field_zone_tendencies = _build_tendencies(field_zone_plays, field_zone_evidence, min_plays=2)
    personnel_tendencies = _build_tendencies(personnel_plays, personnel_evidence, min_plays=2)
    down_distance_tendencies = _build_down_distance_tendencies(
        down_distance_plays,
        down_distance_evidence,
        min_plays=2,
    )
    formation_concept_families = _build_concept_family_distribution(
        concept_family_plays,
        concept_family_evidence,
    )
    motion_tendencies = _build_motion_tendencies(motion_plays, no_motion_plays)
    pre_snap_tells = _build_pre_snap_tells(exposure_plays, exposure_meta, exposure_evidence)
    alerts = _generate_alerts(
        formation_tendencies,
        motion_tendencies,
        field_zone_tendencies,
        personnel_tendencies,
    )

    return SelfScoutResponse(
        formation_tendencies=formation_tendencies,
        motion_tendencies=motion_tendencies,
        field_zone_tendencies=field_zone_tendencies,
        personnel_tendencies=personnel_tendencies,
        down_distance_tendencies=down_distance_tendencies,
        formation_concept_families=formation_concept_families,
        pre_snap_tells=pre_snap_tells,
        alerts=alerts,
        clip_count=len(clips),
    )


def _empty_self_scout_response() -> SelfScoutResponse:
    return SelfScoutResponse(
        formation_tendencies=[],
        motion_tendencies=MotionTendencyResponse(
            with_motion=MotionTendency(
                total=0,
                run_count=0,
                pass_count=0,
                run_rate=0,
                pass_rate=0,
            ),
            without_motion=MotionTendency(
                total=0,
                run_count=0,
                pass_count=0,
                run_rate=0,
                pass_rate=0,
            ),
        ),
        field_zone_tendencies=[],
        personnel_tendencies=[],
        down_distance_tendencies=[],
        formation_concept_families={},
        pre_snap_tells=[],
        alerts=[],
        clip_count=0,
    )


def _get_play_type(labels: list[Label]) -> str | None:
    for lbl in labels:
        lt = lbl.label_type
        lv = lbl.label_value or {}
        if lt == "play_concept":
            concept = _get_label_text(lv)
            if _contains_any_phrase(concept, ("run", "zone", "power", "counter", "draw", "sweep")):
                return "run"
            if _contains_any_phrase(concept, ("pass", "screen", "play_action", "rpo")):
                return "pass"
        if lt == "play_direction":
            return "run"
    return None


def _get_formation(labels: list[Label]) -> str | None:
    for lbl in labels:
        if lbl.label_type == "offensive_formation":
            formation = (lbl.label_value or {}).get("formation")
            if isinstance(formation, str):
                return formation
    return None


def _get_motion_detected(labels: list[Label]) -> bool:
    for lbl in labels:
        if lbl.label_type == "motion_detected":
            return bool((lbl.label_value or {}).get("detected", False))
    return False


def _get_concept_family(labels: list[Label]) -> str | None:
    for lbl in labels:
        if lbl.label_type != "play_concept":
            continue
        concept = _get_label_text(lbl.label_value or {})
        if _contains_any_phrase(concept, ("pass", "screen", "mesh", "smash", "stick", "rpo")):
            return "pass_concept"
        if _contains_any_phrase(concept, ("inside_zone", "inside zone", "duo", "split zone")):
            return "inside_zone"
        if _contains_any_phrase(
            concept,
            ("outside_zone", "outside zone", "wide zone", "stretch", "toss"),
        ):
            return "outside_zone"
        if _contains_any_phrase(concept, ("gap", "power", "counter", "trap", "wham", "dart")):
            return "gap"
    return None


def _get_label_text(label_value: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in label_value.values():
        if isinstance(value, str | int | float | bool):
            parts.append(str(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str | int | float | bool):
                    parts.append(str(item))
    return " ".join(parts).lower().replace("_", " ")


def _contains_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    tokens = set(text.split())
    for phrase in phrases:
        normalized_phrase = phrase.lower().replace("_", " ")
        if " " in normalized_phrase:
            if normalized_phrase in text:
                return True
        elif normalized_phrase in tokens:
            return True
    return False


def _get_distance_bucket(distance: float | None) -> str | None:
    """Bucket yards-to-go into short (1-3), medium (4-6), and long (7+) bands."""
    if distance is None:
        return None
    if distance <= 3:
        return "short"
    if distance <= 6:
        return "medium"
    return "long"


def _get_down_distance_key(clip: Clip) -> tuple[int, str] | None:
    if clip.down is None or clip.down < 1 or clip.down > 4:
        return None
    distance_bucket = _get_distance_bucket(clip.distance)
    if distance_bucket is None:
        return None
    return (clip.down, distance_bucket)


def _build_tendencies(
    plays: dict[str, Counter[str]],
    evidence: dict[str, list[str]] | None = None,
    min_plays: int = 3,
) -> list[TendencyEntry]:
    results: list[TendencyEntry] = []
    for key, counts in plays.items():
        total = sum(counts.values())
        if total < min_plays:
            continue
        results.append(
            TendencyEntry(
                grouping_key=key,
                total_plays=total,
                run_count=counts.get("run", 0),
                pass_count=counts.get("pass", 0),
                run_rate=round(counts.get("run", 0) / total, 3),
                pass_rate=round(counts.get("pass", 0) / total, 3),
                evidence_clip_ids=_limit_evidence((evidence or {}).get(key, [])),
                low_sample=total < LOW_SAMPLE_THRESHOLD,
            )
        )
    return sorted(results, key=lambda r: r.total_plays, reverse=True)


def _build_motion_tendencies(
    motion_plays: Counter[str],
    no_motion_plays: Counter[str],
) -> MotionTendencyResponse:
    motion_total = sum(motion_plays.values())
    no_motion_total = sum(no_motion_plays.values())
    return MotionTendencyResponse(
        with_motion=MotionTendency(
            total=motion_total,
            run_count=motion_plays.get("run", 0),
            pass_count=motion_plays.get("pass", 0),
            run_rate=round(motion_plays.get("run", 0) / max(motion_total, 1), 3),
            pass_rate=round(motion_plays.get("pass", 0) / max(motion_total, 1), 3),
        ),
        without_motion=MotionTendency(
            total=no_motion_total,
            run_count=no_motion_plays.get("run", 0),
            pass_count=no_motion_plays.get("pass", 0),
            run_rate=round(no_motion_plays.get("run", 0) / max(no_motion_total, 1), 3),
            pass_rate=round(no_motion_plays.get("pass", 0) / max(no_motion_total, 1), 3),
        ),
    )


def _build_down_distance_tendencies(
    plays: dict[tuple[int, str], Counter[str]],
    evidence: dict[tuple[int, str], list[str]],
    min_plays: int = 2,
) -> list[DownDistanceTendency]:
    results: list[DownDistanceTendency] = []
    for (down, distance_bucket), counts in plays.items():
        total = sum(counts.values())
        if total < min_plays:
            continue
        results.append(
            DownDistanceTendency(
                down=down,
                distance_bucket=distance_bucket,
                total_plays=total,
                run_count=counts.get("run", 0),
                pass_count=counts.get("pass", 0),
                run_rate=round(counts.get("run", 0) / total, 3),
                pass_rate=round(counts.get("pass", 0) / total, 3),
                evidence_clip_ids=_limit_evidence(evidence.get((down, distance_bucket), [])),
                low_sample=total < LOW_SAMPLE_THRESHOLD,
            )
        )
    return sorted(
        results,
        key=lambda r: (r.down, DISTANCE_BUCKET_ORDER.get(r.distance_bucket, 99)),
    )


def _build_concept_family_distribution(
    plays: dict[str, Counter[str]],
    evidence: dict[tuple[str, str], list[str]],
) -> dict[str, list[ConceptFamilyEntry]]:
    formation_totals = {formation: sum(counts.values()) for formation, counts in plays.items()}
    results: dict[str, list[ConceptFamilyEntry]] = {}
    for formation, _total in sorted(
        formation_totals.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        counts = plays[formation]
        entries: list[ConceptFamilyEntry] = []
        total = sum(counts.values())
        if total == 0:
            continue
        for concept_family, count in counts.items():
            entries.append(
                ConceptFamilyEntry(
                    formation=formation,
                    concept_family=concept_family,
                    total_plays=count,
                    rate=round(count / total, 3),
                    evidence_clip_ids=_limit_evidence(
                        evidence.get((formation, concept_family), [])
                    ),
                    low_sample=count < LOW_SAMPLE_THRESHOLD,
                )
            )
        results[formation] = sorted(entries, key=lambda entry: entry.total_plays, reverse=True)
    return results


def _build_pre_snap_tells(
    plays: dict[str, Counter[str]],
    exposure_meta: dict[str, tuple[str, str]],
    evidence: dict[str, list[str]],
) -> list[ExposureAlert]:
    results: list[ExposureAlert] = []
    for key, counts in plays.items():
        total = sum(counts.values())
        if total == 0:
            continue
        run_rate = round(counts.get("run", 0) / total, 3)
        pass_rate = round(counts.get("pass", 0) / total, 3)
        if run_rate < PRE_SNAP_TELL_THRESHOLD and pass_rate < PRE_SNAP_TELL_THRESHOLD:
            continue
        formation, motion_state = exposure_meta[key]
        lean = "run" if run_rate >= PRE_SNAP_TELL_THRESHOLD else "pass"
        lean_rate = run_rate if lean == "run" else pass_rate
        results.append(
            ExposureAlert(
                grouping_key=key,
                formation=formation,
                motion_state=motion_state,
                total_plays=total,
                lean=lean,
                severity=(
                    "high" if lean_rate >= PRE_SNAP_TELL_HIGH_SEVERITY_THRESHOLD else "medium"
                ),
                run_rate=run_rate,
                pass_rate=pass_rate,
                evidence_clip_ids=_limit_evidence(evidence.get(key, [])),
                low_sample=total < LOW_SAMPLE_THRESHOLD,
                message=(
                    f"Pre-snap tell from {formation} "
                    f"{MOTION_STATE_LABELS.get(motion_state, motion_state.replace('_', ' '))}: "
                    f"{lean_rate:.0%} {lean} ({total} plays)"
                ),
            )
        )
    return sorted(results, key=lambda r: r.total_plays, reverse=True)


def _limit_evidence(clip_ids: list[str]) -> list[str]:
    return clip_ids[:EVIDENCE_CLIP_LIMIT]


def _generate_alerts(
    formation_tendencies: list[TendencyEntry],
    motion_tendencies: MotionTendencyResponse,
    field_zone_tendencies: list[TendencyEntry],
    personnel_tendencies: list[TendencyEntry],
) -> list[TendencyAlert]:
    alerts: list[TendencyAlert] = []

    for ft in formation_tendencies:
        if ft.total_plays >= 5:
            if ft.run_rate >= TENDENCY_ALERT_THRESHOLD:
                alerts.append(
                    TendencyAlert(
                        alert_type="formation_tendency",
                        message=(
                            f"High run tendency from {ft.grouping_key}: "
                            f"{ft.run_rate:.0%} ({ft.total_plays} plays)"
                        ),
                        severity="high" if ft.run_rate >= 0.85 else "medium",
                        grouping_key=ft.grouping_key,
                        run_rate=ft.run_rate,
                        pass_rate=ft.pass_rate,
                    )
                )
            if ft.pass_rate >= TENDENCY_ALERT_THRESHOLD:
                alerts.append(
                    TendencyAlert(
                        alert_type="formation_tendency",
                        message=(
                            f"High pass tendency from {ft.grouping_key}: "
                            f"{ft.pass_rate:.0%} ({ft.total_plays} plays)"
                        ),
                        severity="high" if ft.pass_rate >= 0.85 else "medium",
                        grouping_key=ft.grouping_key,
                        run_rate=ft.run_rate,
                        pass_rate=ft.pass_rate,
                    )
                )

    mt = motion_tendencies.with_motion
    if mt.total >= 5:
        if mt.run_rate >= TENDENCY_ALERT_THRESHOLD:
            alerts.append(
                TendencyAlert(
                    alert_type="motion_tendency",
                    message=f"High run tendency after motion: {mt.run_rate:.0%}",
                    severity="medium",
                    grouping_key="with_motion",
                    run_rate=mt.run_rate,
                    pass_rate=mt.pass_rate,
                )
            )
        if mt.pass_rate >= TENDENCY_ALERT_THRESHOLD:
            alerts.append(
                TendencyAlert(
                    alert_type="motion_tendency",
                    message=f"High pass tendency after motion: {mt.pass_rate:.0%}",
                    severity="medium",
                    grouping_key="with_motion",
                    run_rate=mt.run_rate,
                    pass_rate=mt.pass_rate,
                )
            )

    for zt in field_zone_tendencies:
        if zt.total_plays >= 5:
            if zt.run_rate >= TENDENCY_ALERT_THRESHOLD:
                alerts.append(
                    TendencyAlert(
                        alert_type="field_zone_tendency",
                        message=f"High run tendency in {zt.grouping_key}: {zt.run_rate:.0%}",
                        severity="medium",
                        grouping_key=zt.grouping_key,
                        run_rate=zt.run_rate,
                        pass_rate=zt.pass_rate,
                    )
                )
            if zt.pass_rate >= TENDENCY_ALERT_THRESHOLD:
                alerts.append(
                    TendencyAlert(
                        alert_type="field_zone_tendency",
                        message=f"High pass tendency in {zt.grouping_key}: {zt.pass_rate:.0%}",
                        severity="medium",
                        grouping_key=zt.grouping_key,
                        run_rate=zt.run_rate,
                        pass_rate=zt.pass_rate,
                    )
                )

    for pt in personnel_tendencies:
        if pt.total_plays >= 5:
            if pt.run_rate >= TENDENCY_ALERT_THRESHOLD:
                alerts.append(
                    TendencyAlert(
                        alert_type="personnel_tendency",
                        message=f"High run tendency from {pt.grouping_key}: {pt.run_rate:.0%}",
                        severity="medium",
                        grouping_key=pt.grouping_key,
                        run_rate=pt.run_rate,
                        pass_rate=pt.pass_rate,
                    )
                )
            if pt.pass_rate >= TENDENCY_ALERT_THRESHOLD:
                alerts.append(
                    TendencyAlert(
                        alert_type="personnel_tendency",
                        message=f"High pass tendency from {pt.grouping_key}: {pt.pass_rate:.0%}",
                        severity="medium",
                        grouping_key=pt.grouping_key,
                        run_rate=pt.run_rate,
                        pass_rate=pt.pass_rate,
                    )
                )

    return alerts


# ── Tendency-break alerts (Issue #137) ──────────────────────────────────────────


class TendencyBreakAlertOut(BaseModel):
    """A persisted tendency-break alert, flattened for the frontend overlay."""

    id: uuid.UUID | None = None
    grouping_key: str
    tendency_kind: str
    lean: str
    severity: str
    confidence: float
    message: str
    run_rate: float
    pass_rate: float
    total_plays: int
    evidence_clip_ids: list[str]
    source: str
    experimental: bool
    baseline_pass_rate: float | None = None
    divergence_pp: float | None = None
    baseline_source: str | None = None
    session_id: str | None = None
    is_actioned: bool = False
    actioned_at: str | None = None


class TendencyBreakRunResponse(BaseModel):
    generated: int
    season_tendency_count: int
    pattern_break_count: int
    season_clip_count: int
    game_clip_count: int
    cfbd_baseline_pass_rate: float | None = None
    cfbd_baseline_source: str | None = None
    alerts: list[TendencyBreakAlertOut]


class TendencyBreakListResponse(BaseModel):
    alerts: list[TendencyBreakAlertOut]
    total: int


def _block_non_staff(user: User) -> None:
    """Tendency-break surfaces are coaching-staff only — never player/viewer."""
    if user.role in (UserRole.player, UserRole.viewer):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-scout tendency breaks are coaching-staff only",
        )


async def _load_plays(
    db: AsyncSession,
    video_id: uuid.UUID | None,
    limit: int,
) -> list[tb.Play]:
    """Load cached clips + labels and reduce them to tendency-break plays."""
    q = select(Clip).order_by(Clip.start_time, Clip.id).limit(limit)
    if video_id is not None:
        q = q.where(Clip.video_id == video_id)
    clips = list((await db.execute(q)).scalars().all())
    if not clips:
        return []

    clip_ids = [c.id for c in clips]
    label_rows = await db.execute(select(Label).where(Label.clip_id.in_(clip_ids)))
    labels = list(label_rows.scalars().all())
    labels_by_clip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lbl in labels:
        if lbl.clip_id is not None:
            labels_by_clip[str(lbl.clip_id)].append(
                {"label_type": lbl.label_type, "label_value": lbl.label_value}
            )

    clip_dicts = [
        {
            "id": str(c.id),
            "personnel_grouping": c.personnel_grouping,
            "field_zone": c.field_zone,
            "down": c.down,
            "distance": c.distance,
        }
        for c in clips
    ]
    return tb.build_plays(clip_dicts, labels_by_clip)


async def _cfbd_baseline(db: AsyncSession, team: str | None) -> float | None:
    """Overall pass rate from cached CFBD plays for ``team`` (offense)."""
    if not team:
        return None
    rows = (
        await db.execute(select(CFBDPlay.play_type).where(CFBDPlay.offense == team).limit(5000))
    ).all()
    if not rows:
        return None
    return tb.cfbd_pass_rate_baseline([{"play_type": r[0]} for r in rows])


def _alert_out_from_engine(
    alert: tb.TendencyBreakAlert,
    *,
    alert_id: uuid.UUID | None,
    session_id: str | None,
    is_actioned: bool = False,
    actioned_at: str | None = None,
) -> TendencyBreakAlertOut:
    return TendencyBreakAlertOut(
        id=alert_id,
        grouping_key=alert.grouping_key,
        tendency_kind=alert.tendency_kind,
        lean=alert.lean,
        severity=alert.severity,
        confidence=alert.confidence,
        message=alert.message,
        run_rate=alert.run_rate,
        pass_rate=alert.pass_rate,
        total_plays=alert.total_plays,
        evidence_clip_ids=alert.evidence_clip_ids,
        source=alert.source,
        experimental=alert.experimental,
        baseline_pass_rate=alert.baseline_pass_rate,
        divergence_pp=alert.divergence_pp,
        baseline_source=alert.baseline_source,
        session_id=session_id,
        is_actioned=is_actioned,
        actioned_at=actioned_at,
    )


def _alert_out_from_orm(a: Alert) -> TendencyBreakAlertOut:
    mv = a.metric_value or {}
    return TendencyBreakAlertOut(
        id=a.id,
        grouping_key=str(mv.get("grouping_key", a.metric_name)),
        tendency_kind=str(mv.get("tendency_kind", "season_tendency")),
        lean=str(mv.get("lean", "")),
        severity=a.severity.value,
        confidence=a.confidence,
        message=str(mv.get("message", "")),
        run_rate=float(mv.get("run_rate", 0.0)),
        pass_rate=float(mv.get("pass_rate", 0.0)),
        total_plays=int(mv.get("total_plays", 0)),
        evidence_clip_ids=[str(c) for c in mv.get("evidence_clip_ids", [])],
        source=str(mv.get("source", "toledo_film")),
        experimental=bool(mv.get("experimental", False)),
        baseline_pass_rate=mv.get("baseline_pass_rate"),
        divergence_pp=mv.get("divergence_pp"),
        baseline_source=mv.get("baseline_source"),
        session_id=a.session_id,
        is_actioned=a.is_actioned,
        actioned_at=a.actioned_at.isoformat() if a.actioned_at else None,
    )


def _first_uuid(clip_ids: list[str]) -> uuid.UUID | None:
    for cid in clip_ids:
        try:
            return uuid.UUID(cid)
        except (ValueError, TypeError):
            continue
    return None


@router.post("/tendency-break", response_model=TendencyBreakRunResponse)
async def generate_tendency_break(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_coach_or_above)],
    _capacity: Annotated[Any, Depends(require_workload_capacity("self_scout.tendency_break"))],
    game_video_id: uuid.UUID | None = Query(
        default=None,
        description="Optional film to evaluate for in-game pattern breaks vs. the season baseline.",
    ),
    cfbd_team: str | None = Query(
        default=None,
        description="Cached CFBD team (offense) to derive an informational pass-rate baseline.",
    ),
    limit: int = Query(default=2000, ge=1, le=5000),
) -> TendencyBreakRunResponse:
    """Generate and persist tendency-break alerts from cached/project data.

    Reads the labeled clips the platform already holds (no live video
    processing), runs the deterministic tendency-break engine, and persists
    ``formation_tendency`` alerts. Heavy enough to be workload-gated because it
    scans the whole labeled corpus.
    """
    season_plays = await _load_plays(db, video_id=None, limit=limit)
    season_alerts = tb.season_tendency_alerts(season_plays)

    game_alerts: list[tb.TendencyBreakAlert] = []
    game_clip_count = 0
    if game_video_id is not None:
        game_plays = await _load_plays(db, video_id=game_video_id, limit=limit)
        game_clip_count = len(game_plays)
        if game_plays:
            baseline = tb.season_pass_rate_by_bucket(season_plays)
            game_alerts = tb.pattern_break_alerts(game_plays, baseline)

    cfbd_baseline = await _cfbd_baseline(db, cfbd_team)

    # Dedup against already-persisted tendency alerts (same session + key).
    season_sid = SEASON_SESSION_ID
    game_sid = str(game_video_id) if game_video_id is not None else None
    session_ids = [season_sid] + ([game_sid] if game_sid else [])
    existing_rows = (
        await db.execute(
            select(Alert.session_id, Alert.metric_name).where(
                Alert.alert_type == AlertType.formation_tendency,
                Alert.session_id.in_(session_ids),
            )
        )
    ).all()
    existing = {(r[0], r[1]) for r in existing_rows}

    out: list[TendencyBreakAlertOut] = []
    created = 0
    for engine_alert, session_id in (
        *[(a, season_sid) for a in season_alerts],
        *[(a, game_sid) for a in game_alerts],
    ):
        # Dedup on the truncated name actually stored as ``Alert.metric_name``.
        stored_name = engine_alert.grouping_key[:255]
        if (session_id, stored_name) in existing:
            continue
        existing.add((session_id, stored_name))
        metric_value = engine_alert.metric_value()
        if cfbd_baseline is not None and cfbd_team:
            metric_value["cfbd_baseline_pass_rate"] = cfbd_baseline
            metric_value["cfbd_team"] = cfbd_team
        alert = Alert(
            id=uuid.uuid4(),
            clip_id=_first_uuid(engine_alert.evidence_clip_ids),
            position_group=TENDENCY_POSITION_GROUP,
            alert_type=AlertType.formation_tendency,
            severity=AlertSeverity(engine_alert.severity),
            confidence=engine_alert.confidence,
            metric_name=stored_name,
            metric_value=metric_value,
            session_id=session_id,
            is_acknowledged=False,
            is_actioned=False,
            created_at=datetime.now(UTC),
        )
        db.add(alert)
        await db.flush()
        created += 1
        out.append(_alert_out_from_engine(engine_alert, alert_id=alert.id, session_id=session_id))
        try:
            from app.routers.alerts import AlertResponse

            publish_alert(AlertResponse.from_orm(alert).model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover - best-effort fan-out
            log.warning("tendency_break_sse_publish_failed", error=str(exc))

    log.info(
        "tendency_break_generated",
        season_clip_count=len(season_plays),
        game_clip_count=game_clip_count,
        season_alerts=len(season_alerts),
        pattern_breaks=len(game_alerts),
        created=created,
    )

    return TendencyBreakRunResponse(
        generated=created,
        season_tendency_count=len(season_alerts),
        pattern_break_count=len(game_alerts),
        season_clip_count=len(season_plays),
        game_clip_count=game_clip_count,
        cfbd_baseline_pass_rate=cfbd_baseline,
        cfbd_baseline_source="cfbd" if cfbd_baseline is not None else None,
        alerts=out,
    )


@router.get("/tendency-break", response_model=TendencyBreakListResponse)
async def list_tendency_break(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(get_current_user)],
    actioned: bool | None = Query(default=None),
    session_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> TendencyBreakListResponse:
    """List persisted tendency-break alerts (coaching-staff only).

    These are offense-wide scouting alerts, so — unlike the position-scoped
    alerts list — they are not filtered by the caller's position group.
    """
    _block_non_staff(current_user)

    q = (
        select(Alert)
        .where(Alert.alert_type == AlertType.formation_tendency)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    if actioned is not None:
        q = q.where(Alert.is_actioned == actioned)
    if session_id is not None:
        q = q.where(Alert.session_id == session_id)

    rows = list((await db.execute(q)).scalars().all())
    return TendencyBreakListResponse(
        alerts=[_alert_out_from_orm(a) for a in rows],
        total=len(rows),
    )
