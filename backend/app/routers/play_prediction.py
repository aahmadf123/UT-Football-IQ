"""Pre-snap play-prediction router (Issues #135 / #136).

Surfaces the calibrated run/pass predictor:

* ``POST /api/v1/play-predictions/batch`` — the GPU worker posts a batch of
  computed predictions. Heavy + governance-gated (analyst/coach write +
  workload capacity).
* ``GET  /api/v1/clips/{clip_id}/play-predictions`` — read predictions for a
  clip (coaching-staff read).
* ``GET  /api/v1/play-predictions/calibration`` — reliability diagram + ECE +
  Brier computed from stored predictions whose outcome has been confirmed.
  Powers the MLOps dashboard reliability surface.
* ``PATCH /api/v1/play-predictions/{id}`` — the coach-correction flywheel:
  records ``true_class`` and folds the outcome into the matching per-opponent
  Dirichlet posterior (so the prior stays data-backed).

No mock predictions are ever written: rows always originate from the worker's
ensemble (``POST /batch``) or a coach correction; reliability metrics are only
computed over rows whose ``true_class`` is known.
"""

from __future__ import annotations

import math
import uuid
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.governance import Action, Resource, audit_event, require_policy
from app.models import Clip, OpponentPrior, PlayPrediction, User
from app.workload import WorkloadSnapshot, require_workload_capacity

log = structlog.get_logger(__name__)
router = APIRouter(tags=["play-predictions"])

_VALID_CLASSES = {"run", "pass"}
_DISTANCE_BUCKETS = {"short", "medium", "long"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class PlayPredictionItem(BaseModel):
    model_config = {"protected_namespaces": ()}

    clip_id: uuid.UUID
    signal_vector: dict[str, Any]
    play_id: uuid.UUID | None = None
    opponent_team: str | None = None
    logit_score: float | None = None
    predicted_class: str | None = None
    calibrated_prob: float | None = None
    confidence: float | None = None
    uncertainty: float | None = None
    is_calibrated: bool = False
    model_version_id: uuid.UUID | None = None

    @field_validator("predicted_class")
    @classmethod
    def _check_class(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_CLASSES:
            raise ValueError(f"predicted_class must be one of {sorted(_VALID_CLASSES)}")
        return v


class PlayPredictionBatch(BaseModel):
    predictions: list[PlayPredictionItem]

    @field_validator("predictions")
    @classmethod
    def _non_empty(cls, v: list[PlayPredictionItem]) -> list[PlayPredictionItem]:
        if not v:
            raise ValueError("predictions must be a non-empty list")
        if len(v) > 1000:
            raise ValueError("batch too large (max 1000 predictions)")
        return v


class BatchCreateResponse(BaseModel):
    created: int
    ids: list[uuid.UUID]


class PlayPredictionResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    id: uuid.UUID
    clip_id: uuid.UUID
    play_id: uuid.UUID | None
    opponent_team: str | None
    signal_vector: dict[str, Any]
    logit_score: float | None
    predicted_class: str | None
    true_class: str | None
    confidence: float | None
    calibrated_prob: float | None
    uncertainty: float | None
    is_calibrated: bool
    model_version_id: uuid.UUID | None
    created_at: str

    @classmethod
    def from_orm_row(cls, p: PlayPrediction) -> PlayPredictionResponse:
        return cls(
            id=p.id,
            clip_id=p.clip_id,
            play_id=p.play_id,
            opponent_team=p.opponent_team,
            signal_vector=p.signal_vector,
            logit_score=p.logit_score,
            predicted_class=p.predicted_class,
            true_class=p.true_class,
            confidence=p.confidence,
            calibrated_prob=p.calibrated_prob,
            uncertainty=p.uncertainty,
            is_calibrated=p.is_calibrated,
            model_version_id=p.model_version_id,
            created_at=p.created_at.isoformat(),
        )


class OpponentPriorResponse(BaseModel):
    id: uuid.UUID
    opponent_team: str | None
    down: int | None
    distance_bucket: str | None
    field_zone: str | None
    alpha_pass: float
    alpha_run: float
    n_pass: int
    n_run: int
    pass_probability: float
    observation_count: int
    updated_at: str

    @classmethod
    def from_orm_row(cls, p: OpponentPrior) -> OpponentPriorResponse:
        num = p.alpha_pass + p.n_pass
        den = p.alpha_pass + p.alpha_run + p.n_pass + p.n_run
        return cls(
            id=p.id,
            opponent_team=p.opponent_team,
            down=p.down,
            distance_bucket=p.distance_bucket,
            field_zone=p.field_zone,
            alpha_pass=p.alpha_pass,
            alpha_run=p.alpha_run,
            n_pass=p.n_pass,
            n_run=p.n_run,
            pass_probability=float(num / den) if den > 0 else 0.5,
            observation_count=p.n_pass + p.n_run,
            updated_at=p.updated_at.isoformat(),
        )


class CorrectionUpdate(BaseModel):
    true_class: str

    @field_validator("true_class")
    @classmethod
    def _check_class(cls, v: str) -> str:
        if v not in _VALID_CLASSES:
            raise ValueError(f"true_class must be one of {sorted(_VALID_CLASSES)}")
        return v


class ReliabilityBin(BaseModel):
    bin_lower: float
    bin_upper: float
    mean_predicted: float
    empirical_accuracy: float
    count: int


class CalibrationSummary(BaseModel):
    sample_count: int
    brier_score: float | None
    expected_calibration_error: float | None
    accuracy: float | None
    reliability: list[ReliabilityBin]


# ── Pure-Python calibration metrics (no numpy backend dependency) ──────────────


def _reliability(pairs: list[tuple[float, int]], n_bins: int = 10) -> list[ReliabilityBin]:
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if i == n_bins - 1:
            members = [(p, y) for p, y in pairs if lo <= p <= hi]
        else:
            members = [(p, y) for p, y in pairs if lo <= p < hi]
        if not members:
            continue
        n = len(members)
        bins.append(
            ReliabilityBin(
                bin_lower=lo,
                bin_upper=hi,
                mean_predicted=sum(p for p, _ in members) / n,
                empirical_accuracy=sum(y for _, y in members) / n,
                count=n,
            )
        )
    return bins


def _ece(pairs: list[tuple[float, int]], reliability: list[ReliabilityBin]) -> float:
    n = len(pairs)
    if n == 0:
        return float("nan")
    return sum((b.count / n) * abs(b.empirical_accuracy - b.mean_predicted) for b in reliability)


def _brier(pairs: list[tuple[float, int]]) -> float:
    if not pairs:
        return float("nan")
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _distance_bucket(distance_yards: float) -> str:
    if distance_yards <= 3:
        return "short"
    if distance_yards <= 7:
        return "medium"
    return "long"


def _field_zone(los_x: float | None) -> str | None:
    if los_x is None or math.isnan(los_x):
        return None
    if los_x <= 10:
        return "backed_up"
    if los_x < 50:
        return "own_territory"
    if los_x < 80:
        return "opp_territory"
    return "red_zone"


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/api/v1/play-predictions/batch",
    response_model=BatchCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_predictions_batch(
    body: PlayPredictionBatch,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAY_PREDICTION, Action.WRITE))],
    _capacity: Annotated[
        WorkloadSnapshot, Depends(require_workload_capacity("play_predictions.batch"))
    ],
) -> BatchCreateResponse:
    """Persist a batch of pre-snap predictions produced by the worker ensemble."""
    clip_ids = {item.clip_id for item in body.predictions}
    result = await db.execute(select(Clip.id).where(Clip.id.in_(clip_ids)))
    existing = {row[0] for row in result.all()}
    missing = clip_ids - existing
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "unknown_clip", "missing_clip_ids": [str(c) for c in missing]},
        )

    ids: list[uuid.UUID] = []
    for item in body.predictions:
        row = PlayPrediction(
            id=uuid.uuid4(),
            clip_id=item.clip_id,
            play_id=item.play_id,
            opponent_team=item.opponent_team,
            signal_vector=item.signal_vector,
            logit_score=item.logit_score,
            predicted_class=item.predicted_class,
            confidence=item.confidence,
            calibrated_prob=item.calibrated_prob,
            uncertainty=item.uncertainty,
            is_calibrated=item.is_calibrated,
            model_version_id=item.model_version_id,
        )
        db.add(row)
        ids.append(row.id)

    await db.flush()
    log.info("play_predictions_batch_created", count=len(ids), actor_id=str(current_user.id))
    return BatchCreateResponse(created=len(ids), ids=ids)


@router.get(
    "/api/v1/clips/{clip_id}/play-predictions",
    response_model=list[PlayPredictionResponse],
)
async def list_predictions_for_clip(
    clip_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_policy(Resource.PLAY_PREDICTION, Action.READ))],
) -> list[PlayPredictionResponse]:
    """List stored predictions for a clip (coaching-staff read)."""
    result = await db.execute(
        select(PlayPrediction)
        .where(PlayPrediction.clip_id == clip_id)
        .order_by(PlayPrediction.created_at.desc())
    )
    return [PlayPredictionResponse.from_orm_row(p) for p in result.scalars().all()]


@router.get(
    "/api/v1/play-predictions/calibration",
    response_model=CalibrationSummary,
)
async def calibration_summary(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_policy(Resource.PLAY_PREDICTION, Action.READ))],
    n_bins: Annotated[int, Query(ge=2, le=50)] = 10,
    opponent_team: str | None = None,
) -> CalibrationSummary:
    """Reliability diagram + ECE + Brier over outcome-confirmed predictions."""
    q = select(PlayPrediction).where(
        PlayPrediction.true_class.is_not(None),
        PlayPrediction.calibrated_prob.is_not(None),
    )
    if opponent_team:
        q = q.where(PlayPrediction.opponent_team == opponent_team)
    result = await db.execute(q)
    rows = result.scalars().all()

    pairs: list[tuple[float, int]] = [
        (float(p.calibrated_prob), 1 if p.true_class == "pass" else 0)
        for p in rows
        if p.calibrated_prob is not None and p.true_class in _VALID_CLASSES
    ]
    if not pairs:
        return CalibrationSummary(
            sample_count=0,
            brier_score=None,
            expected_calibration_error=None,
            accuracy=None,
            reliability=[],
        )

    reliability = _reliability(pairs, n_bins=n_bins)
    correct = sum(1 for p, y in pairs if (p >= 0.5) == bool(y))
    return CalibrationSummary(
        sample_count=len(pairs),
        brier_score=_brier(pairs),
        expected_calibration_error=_ece(pairs, reliability),
        accuracy=correct / len(pairs),
        reliability=reliability,
    )


@router.get(
    "/api/v1/play-predictions/opponent-priors",
    response_model=list[OpponentPriorResponse],
)
async def list_opponent_priors(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(require_policy(Resource.PLAY_PREDICTION, Action.READ))],
    opponent_team: str | None = None,
) -> list[OpponentPriorResponse]:
    """List per-opponent situational Dirichlet priors (worker base log-odds)."""
    q = select(OpponentPrior)
    if opponent_team:
        q = q.where(OpponentPrior.opponent_team == opponent_team)
    result = await db.execute(q)
    return [OpponentPriorResponse.from_orm_row(p) for p in result.scalars().all()]


@router.patch(
    "/api/v1/play-predictions/{prediction_id}",
    response_model=PlayPredictionResponse,
)
async def correct_prediction(
    prediction_id: uuid.UUID,
    body: CorrectionUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_policy(Resource.PLAY_PREDICTION, Action.WRITE))],
) -> PlayPredictionResponse:
    """Coach-correction flywheel: record the true class + update the prior.

    Folds the confirmed outcome into the matching per-opponent Dirichlet cell
    so the situational prior becomes (more) data-backed. The cell is derived
    from the prediction's stored ``signal_vector`` down-and-distance signal.
    """
    result = await db.execute(select(PlayPrediction).where(PlayPrediction.id == prediction_id))
    pred = result.scalar_one_or_none()
    if pred is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prediction not found")

    pred.true_class = body.true_class

    # Resolve the situational cell from the down-distance-zone signal.
    down, dist_bucket, zone = _situation_from_signal(pred.signal_vector)
    await _update_opponent_prior(db, pred.opponent_team, down, dist_bucket, zone, body.true_class)

    await db.flush()
    audit_event(
        "audit.play_prediction.corrected",
        actor_id=current_user.id,
        actor_role=current_user.role.value,
        resource=Resource.PLAY_PREDICTION.value,
        action=Action.WRITE.value,
    )
    log.info(
        "play_prediction_corrected",
        prediction_id=str(prediction_id),
        true_class=body.true_class,
    )
    return PlayPredictionResponse.from_orm_row(pred)


def _situation_from_signal(
    signal_vector: dict[str, Any],
) -> tuple[int | None, str | None, str | None]:
    """Pull (down, distance_bucket, field_zone) from the stored signal vector."""
    ddz = signal_vector.get("down_distance_zone", {}) if isinstance(signal_vector, dict) else {}
    if not isinstance(ddz, dict):
        return None, None, None
    down = ddz.get("down")
    bucket = ddz.get("distance_bucket")
    if bucket is None and ddz.get("distance_yards") is not None:
        bucket = _distance_bucket(float(ddz["distance_yards"]))
    zone = ddz.get("field_zone")
    down_int = int(down) if isinstance(down, (int, float)) else None
    bucket_str = bucket if bucket in _DISTANCE_BUCKETS else None
    zone_str = zone if isinstance(zone, str) else None
    return down_int, bucket_str, zone_str


async def _update_opponent_prior(
    db: AsyncSession,
    opponent_team: str | None,
    down: int | None,
    distance_bucket: str | None,
    field_zone: str | None,
    true_class: str,
) -> None:
    """Upsert + increment the matching Dirichlet cell (creates a safe default)."""
    result = await db.execute(
        select(OpponentPrior).where(
            OpponentPrior.opponent_team.is_(opponent_team)
            if opponent_team is None
            else OpponentPrior.opponent_team == opponent_team,
            OpponentPrior.down.is_(down) if down is None else OpponentPrior.down == down,
            OpponentPrior.distance_bucket.is_(distance_bucket)
            if distance_bucket is None
            else OpponentPrior.distance_bucket == distance_bucket,
            OpponentPrior.field_zone.is_(field_zone)
            if field_zone is None
            else OpponentPrior.field_zone == field_zone,
        )
    )
    cell = result.scalar_one_or_none()
    if cell is None:
        cell = OpponentPrior(
            id=uuid.uuid4(),
            opponent_team=opponent_team,
            down=down,
            distance_bucket=distance_bucket,
            field_zone=field_zone,
        )
        db.add(cell)
    if true_class == "pass":
        cell.n_pass = (cell.n_pass or 0) + 1
    else:
        cell.n_run = (cell.n_run or 0) + 1
