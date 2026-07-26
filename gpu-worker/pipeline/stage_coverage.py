"""Stage — Coverage Shell & Leverage Analysis (Phase 2).

Analyses defensive alignment and movement to propose:
  - Press / off identification at snap
  - Inside / outside leverage tracking per defender-receiver pair
  - Coverage shell proposal: man, cover-0, cover-1, cover-2, cover-3,
    cover-4 (quarters), cover-6
  - Separates observed behavior from inferred call (intent modeling is Phase 3)

Heuristic approach:
  1. Identify defender tracklets (opposite side of ball from offense).
  2. At snap, measure each defender's alignment depth and lateral position
     relative to the nearest receiver.
  3. Classify press/off based on cushion distance.
  4. Classify leverage based on lateral offset to nearest receiver.
  5. Classify shell based on safety depth and rotation patterns.

Output: `labels` rows with label_type in {coverage_shell, press_off,
leverage, defender_alignment} written to the backend.
"""

from __future__ import annotations

import math
from typing import Any

import structlog

from pipeline import backend
from pipeline.coverage.gnn_classifier import CoverageClassifier, coverage_bust_flag
from pipeline.spatial import feature_schema as fs

log = structlog.get_logger(__name__)

PRESS_THRESHOLD_YD = 5.0
OFF_THRESHOLD_YD = 7.0
LEVERAGE_THRESHOLD_YD = 1.0
SAFETY_DEEP_THRESHOLD_YD = 12.0


def run(
    clip_id: str,
    tracklets: list[dict[str, Any]],
    events: list[dict[str, Any]],
    fps: float,
    *,
    analytics_safe: bool = False,
    offense_direction: int | None = None,
    classifier: CoverageClassifier | None = None,
) -> dict[str, Any]:
    """Analyse coverage shell and leverage, write labels.

    The coverage shell is classified by :class:`CoverageClassifier` (the GNN
    when ``COVERAGE_GNN_MODEL`` is configured, else the deterministic baseline)
    over the shared spatial graph — but only when ``analytics_safe`` confirms the
    field-frame calibration. Without confirmed calibration the stage falls back
    to the legacy depth heuristic and flags the result uncalibrated, so a number
    is never computed on uncalibrated coordinates (#127). The shell ships with a
    calibrated ``coverage_confidence`` (#139/#146) and a ``coverage_bust_flag``.
    """
    log.info("stage_coverage_start", clip_id=clip_id)

    snap_event = next((e for e in events if e.get("event_type") == "snap"), None)
    snap_frame = snap_event.get("frame_number", 0) if snap_event else 0

    los_x = _estimate_los(tracklets, snap_frame)

    offense, defense = _split_offense_defense(tracklets, snap_frame, los_x)

    labels: list[dict[str, Any]] = []

    # ── Press / off and leverage per defender ──────────────────────────────
    alignment_results: list[dict[str, Any]] = []
    for def_t in defense:
        def_snap = _position_at_frame(def_t, snap_frame)
        if def_snap is None:
            continue

        nearest_off = _nearest_player(def_snap, offense, snap_frame)
        if nearest_off is None:
            continue

        cushion = _distance(def_snap, nearest_off)
        press_off = _classify_press_off(cushion)
        leverage = _classify_leverage(def_snap, nearest_off)

        alignment = {
            "track_id": def_t.get("id", "unknown"),
            "cushion_yards": round(cushion, 1),
            "press_off": press_off,
            "leverage": leverage,
            "confidence": 0.6,
        }
        alignment_results.append(alignment)

        labels.append({
            "label_type": "press_off",
            "label_value": {
                "classification": press_off,
                "cushion_yards": round(cushion, 1),
                "track_id": def_t.get("id", "unknown"),
                "confidence": 0.6,
            },
        })
        labels.append({
            "label_type": "leverage",
            "label_value": {
                "classification": leverage,
                "track_id": def_t.get("id", "unknown"),
                "confidence": 0.55,
            },
        })

    # ── Coverage shell classification (GNN + calibrated uncertainty, #139) ──
    if analytics_safe:
        bust = coverage_bust_flag(defense, snap_frame, fps)
    else:
        bust = {
            "coverage_bust_flag": False,
            "max_displacement_yards": None,
            "divergence_threshold_yards": None,
            "window_frames": None,
            "defenders_measured": 0,
            "busted_track_id": None,
            "reason": "calibration_not_analytics_safe",
        }
    shell_value = _classify_shell(
        defense,
        offense,
        snap_frame,
        los_x,
        analytics_safe=analytics_safe,
        offense_direction=offense_direction,
        classifier=classifier,
    )
    shell = str(shell_value["shell"])
    shell_label: dict[str, Any] = {
        "shell": shell,
        "confidence": shell_value["coverage_confidence"],
        "coverage_confidence": shell_value["coverage_confidence"],
        "calibration_method": shell_value["calibration_method"],
        "is_calibrated": shell_value["is_calibrated"],
        "experimental": shell_value["experimental"],
        "model": shell_value["model"],
        "probabilities": shell_value["probabilities"],
        "uncertainty": shell_value["uncertainty"],
        "defender_count": len(defense),
        "coverage_bust_flag": bust["coverage_bust_flag"],
        "bust_detail": bust,
    }
    labels.append({"label_type": "coverage_shell", "label_value": shell_label})

    # Write labels
    label_ids: list[str] = []
    for lbl in labels:
        try:
            resp = backend.create_label(
                lbl["label_type"],
                lbl["label_value"],
                clip_id=clip_id,
                source="model",
            )
            label_ids.append(resp["id"])
        except Exception as exc:
            log.warning("coverage_label_write_failed", error=str(exc))

    log.info(
        "stage_coverage_done",
        clip_id=clip_id,
        label_count=len(label_ids),
        shell=shell,
        coverage_confidence=round(float(shell_value["coverage_confidence"]), 3),
        calibrated=shell_value["is_calibrated"],
        coverage_bust_flag=bust["coverage_bust_flag"],
    )
    return {
        "label_count": len(label_ids),
        "label_ids": label_ids,
        "shell": shell,
        "coverage_confidence": shell_value["coverage_confidence"],
        "calibration_method": shell_value["calibration_method"],
        "coverage_bust_flag": bust["coverage_bust_flag"],
        "alignments": alignment_results,
        # Entropy of the shell head, carried out so the orchestrator can reduce
        # it with the other heads into the clip's review-queue priority. It was
        # computed here and dropped, which is why nothing was ever queued.
        "uncertainty": shell_value["uncertainty"],
        "is_calibrated": shell_value["is_calibrated"],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _estimate_los(
    tracklets: list[dict[str, Any]], snap_frame: int
) -> float:
    xs: list[float] = []
    for t in tracklets:
        for pt in t.get("track_points", []):
            if pt.get("frame_number") == snap_frame:
                fx = pt.get("field_x")
                if fx is not None:
                    xs.append(float(fx))
                break
    if not xs:
        return 50.0
    xs.sort()
    return xs[len(xs) // 2]


def _position_at_frame(
    tracklet: dict[str, Any], frame: int
) -> tuple[float, float] | None:
    for pt in tracklet.get("track_points", []):
        if pt.get("frame_number") == frame:
            fx = pt.get("field_x")
            fy = pt.get("field_y")
            if fx is not None and fy is not None:
                return (float(fx), float(fy))
    return None


def _split_offense_defense(
    tracklets: list[dict[str, Any]],
    snap_frame: int,
    los_x: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split tracklets into offense (behind LOS) and defense (ahead of LOS)."""
    offense: list[dict[str, Any]] = []
    defense: list[dict[str, Any]] = []
    for t in tracklets:
        pos = _position_at_frame(t, snap_frame)
        if pos is None:
            continue
        # Use side_of_ball if available, otherwise heuristic
        side = t.get("side_of_ball")
        if side == "offense":
            offense.append(t)
        elif side == "defense":
            defense.append(t)
        else:
            # Heuristic: offense is behind the LOS cluster center
            if pos[0] < los_x:
                offense.append(t)
            else:
                defense.append(t)
    return offense, defense


def _nearest_player(
    pos: tuple[float, float],
    tracklets: list[dict[str, Any]],
    frame: int,
) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    best_dist = float("inf")
    for t in tracklets:
        p = _position_at_frame(t, frame)
        if p is None:
            continue
        d = _distance(pos, p)
        if d < best_dist:
            best_dist = d
            best = p
    return best


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _classify_press_off(cushion: float) -> str:
    if cushion <= PRESS_THRESHOLD_YD:
        return "press"
    if cushion <= OFF_THRESHOLD_YD:
        return "soft"
    return "off"


def _classify_leverage(
    defender: tuple[float, float], receiver: tuple[float, float]
) -> str:
    """Classify inside/outside/head-up leverage."""
    lateral_diff = abs(defender[1]) - abs(receiver[1])
    if abs(lateral_diff) < LEVERAGE_THRESHOLD_YD:
        return "head_up"
    # Inside leverage = closer to the middle of the field
    if lateral_diff < 0:
        return "inside"
    return "outside"


def _classify_shell(
    defense: list[dict[str, Any]],
    offense: list[dict[str, Any]],
    snap_frame: int,
    los_x: float,
    *,
    analytics_safe: bool,
    offense_direction: int | None,
    classifier: CoverageClassifier | None,
) -> dict[str, Any]:
    """Classify the coverage shell with calibrated uncertainty (Issue #139).

    Uses the GNN/baseline classifier over the shared spatial graph when
    calibration is confirmed; otherwise falls back to the legacy depth heuristic
    and flags the result uncalibrated (coordinates unconfirmed).

    An unresolved ``offense_direction`` takes the same fallback. Confirmed
    coordinates are not enough on their own: ``depth_behind_los`` is signed by
    the attacking direction, so guessing it puts every defender's depth the
    wrong way round and the shell is read off a mirror image of the play.
    """
    if analytics_safe and offense_direction is not None:
        try:
            receiver_positions = [
                pos
                for pos in (_position_at_frame(t, snap_frame) for t in offense)
                if pos is not None
            ]
            nodes = fs.build_player_nodes(
                defense,
                snap_frame,
                los_x,
                calibration_confirmed=True,
                offense_direction=offense_direction,
                receiver_positions=receiver_positions,
            )
            graph = fs.build_spatial_graph(nodes)
            clf = classifier or CoverageClassifier.from_env()
            out = clf.classify(nodes, graph).to_dict()
            return {
                "shell": out["value"],
                "coverage_confidence": out["confidence"],
                "calibration_method": out["calibration_method"],
                "is_calibrated": out["is_calibrated"],
                "experimental": out["experimental"],
                "uncertainty": out["uncertainty"],
                "probabilities": out.get("probabilities"),
                "model": out.get("detail", {}).get("model"),
            }
        except fs.FieldFrameError:
            log.warning("coverage_shell_field_frame_guard_tripped")

    shell, conf = _classify_coverage_shell(defense, snap_frame, los_x)
    return {
        "shell": shell,
        # Discount the heuristic confidence because the field frame is unconfirmed.
        "coverage_confidence": round(conf * 0.8, 4),
        "calibration_method": "uncalibrated",
        "is_calibrated": False,
        "experimental": True,
        "uncertainty": None,
        "probabilities": None,
        "model": "coverage-baseline-heuristic-unconfirmed-coords",
    }


def _classify_coverage_shell(
    defense: list[dict[str, Any]],
    snap_frame: int,
    los_x: float,
) -> tuple[str, float]:
    """Classify coverage shell from safety depth and defender distribution."""
    if not defense:
        return ("unknown", 0.3)

    deep_defenders = 0
    box_defenders = 0

    for t in defense:
        pos = _position_at_frame(t, snap_frame)
        if pos is None:
            continue
        depth = pos[0] - los_x
        if depth > SAFETY_DEEP_THRESHOLD_YD:
            deep_defenders += 1
        elif depth < 5.0:
            box_defenders += 1

    total = len(defense)

    if deep_defenders == 0:
        return ("cover_0", 0.55)
    if deep_defenders == 1 and box_defenders >= 5:
        return ("cover_1", 0.6)
    if deep_defenders == 2 and box_defenders <= 5:
        return ("cover_2", 0.6)
    if deep_defenders == 3:
        return ("cover_3", 0.6)
    if deep_defenders >= 4:
        return ("cover_4", 0.55)

    # Fallback: try to infer man vs zone from alignment patterns
    if total >= 5 and deep_defenders == 1:
        return ("man", 0.5)

    return ("zone", 0.45)
