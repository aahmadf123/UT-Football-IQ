"""Counterfactual coverage simulator — offline/backend MVP read surface (Issue #141).

Answers "this route gained Y yards vs <coverage> — what would it have done vs
another coverage?" from a deterministic lookup + empirical-Bayes regression over
the labeled corpus the platform already holds. The engine lives in
``app.analytics.counterfactual`` (a backend port of the offline GPU-worker
engine, ``pipeline.counterfactual.lookup_simulator``).

Boundaries this router respects (Issue #141 readiness review + constraints):

* **Offline / backend-only.** There is **no** coach-facing "What-if" frontend
  surface — it stays blocked until calibrated uncertainty (#146) and the IA
  decisions (#184/#185/#186) land. This endpoint is coaching-staff only.
* **Experimental, never trusted truth.** Every response is ``experimental`` with
  ``trusted_for_coaching=False`` and prominent uncertainty bands. Low-confidence
  identity / tracking / calibration (caller-supplied) forces experimental-only,
  concept-level language and is recorded in ``low_confidence_inputs``.
* **Concept-level, identity-safe.** Estimates are keyed by route / coverage
  concepts — never named players.
* **Honest about data.** A route with no measured reps returns
  ``data_sufficiency="insufficient"`` and no outcomes rather than a fabricated
  number. No mock data is ever presented as real.
* **Dark-launch + workload-gated.** The whole surface 404s when the feature flag
  is off, and the simulate call is workload-gated because it scans the corpus.
* **American football only.** Route/coverage inputs run through the soccer guard.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import counterfactual as cf
from app.concept_lexicon import detect_soccer_terms
from app.config import get_settings
from app.database import get_db
from app.governance import Action, Resource, audit_event, require_policy
from app.models import Clip, Label, Metric, User
from app.workload import require_workload_capacity

log = structlog.get_logger(__name__)


async def _require_enabled() -> None:
    """Dark-launch guard — 404 the whole surface when the feature is disabled."""
    if not get_settings().counterfactual_simulator_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Counterfactual simulator is disabled",
        )


router = APIRouter(
    prefix="/api/v1/counterfactuals",
    tags=["counterfactuals"],
    dependencies=[Depends(_require_enabled)],
)

_StaffUser = Annotated[User, Depends(require_policy(Resource.COUNTERFACTUAL, Action.READ))]

# Coach-readable definitions surfaced with every response.
DEFINITIONS: dict[str, str] = {
    "what": (
        "Counterfactual coverage simulator (MVP): expected-yards distribution for a "
        "route against alternative coverages, from historical reps."
    ),
    "how_to_read": (
        "expected_yards is the empirical-Bayes mean; confidence_band is the 95% band on "
        "that mean; p10/p50/p90 show the spread. Sparse cells are shrunk toward the route "
        "prior and flagged."
    ),
    "trust": (
        "EXPERIMENTAL. Directional only, never a standalone coaching recommendation, and "
        "never validated Toledo truth until coach-accepted (high false-positive cost)."
    ),
}

_COUNTERFACTUAL_LABEL_TYPES = (
    "play_concept",
    "route_concept",
    "coverage_shell",
    "defensive_shell",
    "coverage",
    "yards_gained",
    "play_outcome",
    "play_result",
)
_COUNTERFACTUAL_OUTCOME_METRIC_NAMES = ("pre_contact_yards", "post_contact_yards")


# ── Schemas ───────────────────────────────────────────────────────────────────


class InputQuality(BaseModel):
    """Caller-supplied confidence in the play's CV substrate (optional).

    Low values do not change the *estimate* (which is concept-level), but they
    force experimental-only language and are recorded in ``low_confidence_inputs``
    so a shaky identity / track / calibration never reads as trusted.
    """

    identity_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    tracking_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    calibration_confirmed: bool | None = None


class CounterfactualRequest(BaseModel):
    route_concept: str = Field(min_length=1, max_length=128)
    factual_coverage: str | None = Field(default=None, max_length=64)
    candidate_coverages: list[str] | None = Field(default=None, max_length=12)
    factual_yards: float | None = None
    top_n: int = Field(default=3, ge=1, le=9)
    video_id: uuid.UUID | None = None
    input_quality: InputQuality | None = None


class CounterfactualResponse(BaseModel):
    route_concept: str
    factual_coverage: str | None
    factual: dict[str, Any] | None
    factual_observed_yards: float | None
    outcomes: list[dict[str, Any]]
    data_sufficiency: str
    route_sample_size: int
    corpus_size: int
    source: str
    experimental: bool
    trusted_for_coaching: bool
    coach_language: str
    low_confidence_inputs: list[str]
    definitions: dict[str, str]
    note: str


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("", response_model=CounterfactualResponse)
async def simulate_counterfactual(
    body: CounterfactualRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: _StaffUser,
    _capacity: Annotated[Any, Depends(require_workload_capacity("counterfactual.simulate"))],
) -> CounterfactualResponse:
    """Rank counterfactual coverages for a route — experimental, staff-only."""
    _soccer_guard(body.route_concept, body.factual_coverage, *(body.candidate_coverages or []))

    settings = get_settings()
    observations = await _load_observations(
        db, video_id=body.video_id, limit=settings.counterfactual_corpus_max_plays
    )
    lookup = cf.CounterfactualLookup.from_observations(observations)
    result = lookup.simulate(
        body.route_concept,
        factual_coverage=body.factual_coverage,
        candidate_coverages=body.candidate_coverages,
        factual_yards=body.factual_yards,
        top_n=body.top_n,
    )

    low_confidence_inputs = _low_confidence_inputs(
        body.input_quality,
        result.data_sufficiency,
        threshold=settings.counterfactual_input_confidence_threshold,
    )

    audit_event(
        "audit.counterfactual.simulated",
        actor_id=user.id,
        actor_role=user.role.value,
        route_concept=result.route_concept,
        coverage_type=result.factual_coverage,
        count=len(result.outcomes),
        data_sufficiency=result.data_sufficiency,
        experimental=True,
    )

    payload = result.to_dict()
    return CounterfactualResponse(
        route_concept=payload["route_concept"],
        factual_coverage=payload["factual_coverage"],
        factual=payload["factual"],
        factual_observed_yards=payload["factual_observed_yards"],
        outcomes=payload["outcomes"],
        data_sufficiency=payload["data_sufficiency"],
        route_sample_size=payload["route_sample_size"],
        corpus_size=len(observations),
        source=payload["source"],
        # The whole surface is experimental and is never a trusted coaching truth.
        experimental=True,
        trusted_for_coaching=False,
        coach_language="experimental_only",
        low_confidence_inputs=low_confidence_inputs,
        definitions=DEFINITIONS,
        note=payload["note"],
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _low_confidence_inputs(
    quality: InputQuality | None, data_sufficiency: str, *, threshold: float
) -> list[str]:
    reasons: list[str] = []
    if quality is not None:
        if quality.identity_confidence is not None and quality.identity_confidence <= threshold:
            reasons.append("identity")
        if quality.tracking_confidence is not None and quality.tracking_confidence <= threshold:
            reasons.append("tracking")
        if quality.calibration_confirmed is False:
            reasons.append("calibration")
    if data_sufficiency != cf.SUFF_SUFFICIENT:
        reasons.append("sparse_sample")
    return reasons


async def _load_observations(
    db: AsyncSession, *, video_id: uuid.UUID | None, limit: int
) -> list[cf.PlayObservation]:
    """Load cached clips + labels + metrics and reduce them to observations."""
    q = select(Clip).order_by(Clip.start_time, Clip.id).limit(limit)
    if video_id is not None:
        q = q.where(Clip.video_id == video_id)
    clips = list((await db.execute(q)).scalars().all())
    if not clips:
        return []

    clip_ids = [c.id for c in clips]

    label_rows = list(
        (
            await db.execute(
                select(Label).where(
                    Label.clip_id.in_(clip_ids),
                    Label.label_type.in_(_COUNTERFACTUAL_LABEL_TYPES),
                )
            )
        )
        .scalars()
        .all()
    )
    labels_by_clip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lbl in label_rows:
        if lbl.clip_id is not None:
            labels_by_clip[str(lbl.clip_id)].append(
                {"label_type": lbl.label_type, "label_value": lbl.label_value}
            )

    metric_rows = list(
        (
            await db.execute(
                select(Metric).where(
                    Metric.clip_id.in_(clip_ids),
                    Metric.metric_name.in_(_COUNTERFACTUAL_OUTCOME_METRIC_NAMES),
                    Metric.is_suppressed.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    metrics_by_clip: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in metric_rows:
        if m.clip_id is not None:
            metrics_by_clip[str(m.clip_id)].append(
                {"metric_name": m.metric_name, "metric_value": m.metric_value}
            )

    clip_rows = [{"id": str(c.id)} for c in clips]
    return cf.build_observations(clip_rows, labels_by_clip, metrics_by_clip)
