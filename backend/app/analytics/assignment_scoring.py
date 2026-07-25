"""Rule-based assignment execution scoring (Issue #15).

A pure, dependency-free engine (no ML, no ORM, no DB, no I/O) that scores how
well a player executed an *assignment* from a coach-authored playbook concept,
given whatever Phase-CV substrate happens to exist for the play. It is the
``assignment_scoring`` sibling of ``app.analytics.tendency_break`` — the router
(`app.routers.playbook`) adapts ORM rows into the dataclasses below and persists
the results.

Design constraints (from Issue #15 and its backlog comments):

* **No fabrication.** When the substrate needed to verify an assignment is
  missing or weak, the result is ``needs_review`` with an explicit reason code —
  never an invented "executed correctly / incorrectly" grade.
* **Identity-aware.** A low-confidence or conflicting player identity downgrades
  the result to ``needs_review``; a player is *never* graded "off assignment"
  on an unreliable identity (Toledo film carries jersey-OCR ambiguity).
* **Calibration-aware.** Rules that need yard-accurate geometry require a
  calibrated trajectory; without it they degrade rather than guess. This is the
  graceful dependency on calibrated tracking (#127/#128/#129) — it is assumed
  *optionally*, never required.
* **Confidence + reason codes.** Every result carries a 0–1 confidence, its
  complementary uncertainty, and machine-readable reason codes so the UI can
  explain the grade and avoid definitive claims until validated.
* **Experimental by default.** Output is flagged experimental unless the concept
  was coach-validated *and* the identity is ``known`` — matching "mark outputs
  experimental until validated against corrected Toledo clips".

The scoring rule for an assignment is intentionally separate from its
``intended_route`` overlay geometry: the route is what the coach draws on film;
the rule is the small deterministic check used to grade execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── Grades ──────────────────────────────────────────────────────────────────
GRADE_ON = "on_assignment"
GRADE_OFF = "off_assignment"
GRADE_REVIEW = "needs_review"

# Identity states confident enough to assign a pass/fail grade. Mirrors
# ``app.models.PlayerIdentityState`` — anything else (unknown / conflicting /
# needs_review / missing) is treated as not-confident.
CONFIDENT_IDENTITY_STATES = frozenset({"known", "probable"})

# ── Reason codes ────────────────────────────────────────────────────────────
REASON_NO_MAPPING = "no_player_mapping"
REASON_TRACKLET_NOT_FOUND = "tracklet_not_found"
REASON_LOW_IDENTITY = "low_identity_confidence"
REASON_NO_TRAJECTORY = "no_trajectory"
REASON_NO_CALIBRATED_TRAJECTORY = "no_calibrated_trajectory"
REASON_MANUAL_ONLY = "manual_grade_required"
REASON_UNKNOWN_RULE = "unknown_rule"
REASON_MATCHED = "matched_intended_assignment"
REASON_DIVERGED = "diverged_from_intended_assignment"
REASON_UNVALIDATED_CONCEPT = "model_identified_concept_unvalidated"
REASON_PROBABLE_IDENTITY = "probable_identity_not_confirmed"

# Default pass threshold for soft (0–1) assignment scores.
DEFAULT_PASS_SCORE = 0.6
# Confidence used for a present-but-unscored track-quality signal.
_DEFAULT_TRACK_CONFIDENCE = 0.6


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# ── Inputs ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TrajectoryPoint:
    """One sampled point of a player's track.

    ``norm_x`` / ``norm_y`` are frame-normalized in ``[0, 1]`` (origin top-left)
    when the track point can be projected into video space. ``field_x`` /
    ``field_y`` are yard coordinates and are ``None`` unless field calibration
    produced them.
    """

    norm_x: float | None
    norm_y: float | None
    field_x: float | None = None
    field_y: float | None = None


@dataclass(frozen=True)
class AssignmentDefinition:
    """One assignment within a concept (coach-authored)."""

    key: str
    role: str
    intent: str
    coaching_point: str
    rule: dict[str, Any]
    intended_route: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConceptDefinition:
    name: str
    side: str  # "offense" | "defense"
    structure_kind: str
    assignments: list[AssignmentDefinition]
    coaching_points: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssignmentSubstrate:
    """Resolved CV substrate for one assignment on one play.

    ``resolution`` records how the assignment→player mapping resolved:
    ``"ok"`` (a tracklet was found), ``"no_mapping"`` (the link carries no entry
    for this assignment), or ``"tracklet_not_found"`` (an entry exists but the
    tracklet row is gone).
    """

    assignment_key: str
    resolution: str = "no_mapping"
    tracklet_id: str | None = None
    identity_state: str | None = None
    identity_confidence: float | None = None
    track_confidence: float | None = None
    calibration_confidence: float | None = None
    has_calibrated_trajectory: bool = False
    trajectory: list[TrajectoryPoint] = field(default_factory=list)
    concept_source: str = "coach"  # "coach" | "model"
    concept_validated: bool = False


# ── Results ─────────────────────────────────────────────────────────────────


@dataclass
class AssignmentScoreResult:
    assignment_key: str
    grade: str
    score: float | None
    confidence: float
    uncertainty: float
    reason_codes: list[str]
    experimental: bool
    detail: dict[str, Any]


@dataclass
class PlayExecutionResult:
    play_score: float | None
    graded_count: int
    on_count: int
    off_count: int
    needs_review_count: int
    confidence: float
    experimental: bool
    assignments: list[AssignmentScoreResult]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _base_experimental(sub: AssignmentSubstrate) -> bool:
    """A graded result is non-experimental only for a coach-validated concept on
    a ``known`` identity — everything else stays experimental until validated."""
    return not (
        sub.concept_validated and sub.concept_source == "coach" and sub.identity_state == "known"
    )


def _signal_confidence(sub: AssignmentSubstrate, *, uses_calibration: bool) -> float:
    identity = sub.identity_confidence if sub.identity_confidence is not None else 0.0
    track = sub.track_confidence if sub.track_confidence is not None else _DEFAULT_TRACK_CONFIDENCE
    calib = 1.0
    if uses_calibration:
        calib = sub.calibration_confidence if sub.calibration_confidence is not None else 0.0
    return _clamp01(identity * track * calib)


def _review(
    key: str,
    reason: str,
    *,
    confidence: float = 0.0,
    experimental: bool = True,
    detail: dict[str, Any] | None = None,
) -> AssignmentScoreResult:
    return AssignmentScoreResult(
        assignment_key=key,
        grade=GRADE_REVIEW,
        score=None,
        confidence=round(_clamp01(confidence), 3),
        uncertainty=round(1.0 - _clamp01(confidence), 3),
        reason_codes=[reason],
        experimental=experimental,
        detail=detail or {},
    )


def _graded(
    key: str,
    *,
    score: float,
    confidence: float,
    experimental: bool,
    extra_reasons: list[str],
    detail: dict[str, Any],
    pass_score: float,
) -> AssignmentScoreResult:
    score = round(_clamp01(score), 3)
    on = score >= pass_score
    reasons = [REASON_MATCHED if on else REASON_DIVERGED, *extra_reasons]
    return AssignmentScoreResult(
        assignment_key=key,
        grade=GRADE_ON if on else GRADE_OFF,
        score=score,
        confidence=round(_clamp01(confidence), 3),
        uncertainty=round(1.0 - _clamp01(confidence), 3),
        reason_codes=reasons,
        experimental=experimental,
        detail=detail,
    )


# ── Rule implementations ────────────────────────────────────────────────────
#
# Each rule returns a soft score in [0, 1] plus a detail dict, or ``None`` when
# the substrate is insufficient (the caller turns that into needs_review).


def _rule_release_direction(
    traj: list[TrajectoryPoint], params: dict[str, Any]
) -> tuple[float, dict[str, Any]] | tuple[None, str]:
    """Did the player release along the expected frame axis/sign?

    params: ``axis`` ("x"|"y"), ``sign`` (+1|-1), ``min_delta`` (normalized,
    default 0.05), ``window`` (fraction of the trajectory to inspect, default
    0.4). Orientation-agnostic: the coach picks the axis/sign for the look.
    """
    coords = [(p.norm_x, p.norm_y) for p in traj if p.norm_x is not None and p.norm_y is not None]
    if len(coords) < 2:
        return None, REASON_NO_TRAJECTORY
    axis = str(params.get("axis", "y"))
    sign = 1.0 if float(params.get("sign", 1)) >= 0 else -1.0
    min_delta = float(params.get("min_delta", 0.05))
    window = _clamp01(float(params.get("window", 0.4)))
    end_idx = max(1, round(window * (len(coords) - 1)))
    start_x, start_y = coords[0]
    end_x, end_y = coords[end_idx]
    delta = (end_x - start_x) if axis == "x" else (end_y - start_y)
    signed = delta * sign
    score = signed / (min_delta * 3.0) if min_delta > 0 else 0.0
    return _clamp01(score), {"signed_delta": round(signed, 4), "axis": axis}


def _rule_zone_landmark(
    traj: list[TrajectoryPoint], params: dict[str, Any]
) -> tuple[float, dict[str, Any]] | tuple[None, str]:
    """Did the player finish inside the expected normalized region (zone drop)?

    params: ``region`` = ``{"x": [lo, hi], "y": [lo, hi]}`` in ``[0, 1]``.
    Score is 1.0 inside the box and falls off linearly with distance outside it.
    """
    coords = [(p.norm_x, p.norm_y) for p in traj if p.norm_x is not None and p.norm_y is not None]
    if not coords:
        return None, REASON_NO_TRAJECTORY
    region = params.get("region") or {}
    rx = region.get("x", [0.0, 1.0])
    ry = region.get("y", [0.0, 1.0])
    end_x, end_y = coords[-1]
    dx = max(0.0, rx[0] - end_x, end_x - rx[1])
    dy = max(0.0, ry[0] - end_y, end_y - ry[1])
    dist = (dx * dx + dy * dy) ** 0.5
    tolerance = float(params.get("tolerance", 0.25))
    score = 1.0 - (dist / tolerance if tolerance > 0 else 1.0)
    return _clamp01(score), {"end": [round(end_x, 3), round(end_y, 3)]}


def _rule_depth_threshold(
    traj: list[TrajectoryPoint], params: dict[str, Any]
) -> tuple[float, dict[str, Any]] | tuple[None, str]:
    """Did the player get the required downfield depth (yards)? Needs calibration.

    params: ``min_depth_yards`` (default 12.0).
    """
    field_points = [p for p in traj if p.field_y is not None]
    if len(field_points) < 2:
        return None, REASON_NO_CALIBRATED_TRAJECTORY
    min_depth = float(params.get("min_depth_yards", 12.0))
    ys = [p.field_y for p in field_points if p.field_y is not None]
    depth = max(ys) - min(ys)
    score = depth / min_depth if min_depth > 0 else 0.0
    return _clamp01(score), {"depth_yards": round(depth, 2), "min_depth_yards": min_depth}


def _rule_presence(
    traj: list[TrajectoryPoint], params: dict[str, Any]
) -> tuple[float, dict[str, Any]] | tuple[None, str]:
    """Was the player tracked for enough of the play? params: ``min_points``."""
    min_points = int(params.get("min_points", 3))
    if not traj:
        return None, REASON_NO_TRAJECTORY
    score = len(traj) / min_points if min_points > 0 else 1.0
    return _clamp01(score), {"points": len(traj)}


_RULES = {
    "release_direction": (_rule_release_direction, False),
    "zone_landmark": (_rule_zone_landmark, False),
    "depth_threshold": (_rule_depth_threshold, True),
    "presence": (_rule_presence, False),
}


# ── Public API ──────────────────────────────────────────────────────────────


def score_assignment(
    defn: AssignmentDefinition,
    sub: AssignmentSubstrate,
    *,
    identity_confidence_threshold: float,
) -> AssignmentScoreResult:
    """Grade a single assignment from its definition and resolved substrate."""
    # 1. Mapping / tracklet resolution.
    if sub.resolution == "no_mapping" or sub.tracklet_id is None:
        return _review(defn.key, REASON_NO_MAPPING)
    if sub.resolution == "tracklet_not_found":
        return _review(defn.key, REASON_TRACKLET_NOT_FOUND)

    # 2. Identity gate — never grade a wrong assignment on a shaky identity.
    identity_ok = (
        sub.identity_state in CONFIDENT_IDENTITY_STATES
        and sub.identity_confidence is not None
        and sub.identity_confidence >= identity_confidence_threshold
    )
    if not identity_ok:
        return _review(
            defn.key,
            REASON_LOW_IDENTITY,
            confidence=sub.identity_confidence or 0.0,
            detail={
                "identity_state": sub.identity_state,
                "identity_confidence": sub.identity_confidence,
            },
        )

    rule = defn.rule or {}
    rule_type = str(rule.get("type", "manual_only"))
    params = rule.get("params") or {}
    extra_reasons: list[str] = []
    experimental = _base_experimental(sub)
    if sub.concept_source == "model" and not sub.concept_validated:
        extra_reasons.append(REASON_UNVALIDATED_CONCEPT)
    if sub.identity_state == "probable":
        extra_reasons.append(REASON_PROBABLE_IDENTITY)

    # 3. Manual-only assignments are always coach-graded.
    if rule_type == "manual_only":
        return _review(
            defn.key,
            REASON_MANUAL_ONLY,
            confidence=_signal_confidence(sub, uses_calibration=False),
            experimental=experimental,
        )

    if rule_type not in _RULES:
        return _review(defn.key, REASON_UNKNOWN_RULE, experimental=experimental)

    rule_fn, uses_calibration = _RULES[rule_type]
    try:
        outcome = rule_fn(sub.trajectory, params)
        pass_score = float(rule.get("pass_score", DEFAULT_PASS_SCORE))
    except (IndexError, KeyError, TypeError, ValueError):
        return _review(defn.key, REASON_UNKNOWN_RULE, experimental=experimental)
    raw_score, info = outcome
    if raw_score is None:
        # ``info`` carries the reason code for the missing substrate.
        return _review(defn.key, str(info), experimental=experimental)

    confidence = _signal_confidence(sub, uses_calibration=uses_calibration)
    detail: dict[str, Any] = {"rule": rule_type}
    if isinstance(info, dict):
        detail.update(info)
    return _graded(
        defn.key,
        score=raw_score,
        confidence=confidence,
        experimental=experimental,
        extra_reasons=extra_reasons,
        detail=detail,
        pass_score=pass_score,
    )


def score_play(
    concept: ConceptDefinition,
    substrates: dict[str, AssignmentSubstrate],
    *,
    identity_confidence_threshold: float,
) -> PlayExecutionResult:
    """Score every assignment in a concept and roll up a play-level summary.

    ``substrates`` maps ``assignment_key`` → its resolved substrate. Missing keys
    default to an unmapped substrate (→ ``needs_review``).
    """
    results: list[AssignmentScoreResult] = []
    for defn in concept.assignments:
        sub = substrates.get(defn.key, AssignmentSubstrate(assignment_key=defn.key))
        results.append(
            score_assignment(defn, sub, identity_confidence_threshold=identity_confidence_threshold)
        )
    return summarize_play(results)


def summarize_play(results: list[AssignmentScoreResult]) -> PlayExecutionResult:
    """Roll up a play-level summary from assignment results."""
    graded = [r for r in results if r.grade in (GRADE_ON, GRADE_OFF)]
    on_count = sum(1 for r in results if r.grade == GRADE_ON)
    off_count = sum(1 for r in results if r.grade == GRADE_OFF)
    review_count = sum(1 for r in results if r.grade == GRADE_REVIEW)

    play_score = round(sum(r.score or 0.0 for r in graded) / len(graded), 3) if graded else None
    confidence = round(sum(r.confidence for r in graded) / len(graded), 3) if graded else 0.0
    experimental = bool(review_count) or any(r.experimental for r in graded) or not graded

    return PlayExecutionResult(
        play_score=play_score,
        graded_count=len(graded),
        on_count=on_count,
        off_count=off_count,
        needs_review_count=review_count,
        confidence=confidence,
        experimental=experimental,
        assignments=results,
    )
