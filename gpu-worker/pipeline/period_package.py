"""Per-period summary builder for same-session feedback (Issue #16).

Aggregates effort metrics, formation labels, and identity conflict flags from
all clips in a practice period into a single JSON package that can be pushed to
the coaching app via the SSE endpoint.

Biomechanical deviation checks are silently skipped when pose data is
unavailable — the package is still returned.

Alert governance:
  - All generated alerts follow the evidence-first principle.
  - Bio deviation alerts require pose_pipeline_active=True AND
    coach_approved=True in the alert_config for the relevant position group.
  - Alerts are never shown to players.

Public interface:

    from pipeline.period_package import build

    package = build(
        period_name="Period 3",
        session_id="2026-05-08-practice",
        clips=[...],
        tracklets=[...],
        metrics=[...],
        labels=[...],
        identity_conflicts=[...],
        pose_metrics=[...],           # optional
        baseline_metrics_by_player={...},  # optional
        alert_config={...},           # optional
    )
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from alerts import bio_deviation_alert, effort_anomaly

log = structlog.get_logger(__name__)


def build(
    period_name: str,
    session_id: str,
    clips: list[dict[str, Any]],
    tracklets: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    identity_conflicts: list[dict[str, Any]],
    *,
    pose_metrics: list[dict[str, Any]] | None = None,
    baseline_metrics_by_player: dict[str, list[dict[str, Any]]] | None = None,
    alert_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a per-period summary package for the SSE push.

    Args:
        period_name:               Practice period label (e.g. "Period 3").
        session_id:                Practice session identifier.
        clips:                     Clip dicts processed in this period.
        tracklets:                 All tracklets for this period.
        metrics:                   All computed metrics for this period.
        labels:                    Formation / motion labels for this period.
        identity_conflicts:        Identity conflict flags from Re-ID stage.
        pose_metrics:              Optional biomechanics metrics (Issue #6).
        baseline_metrics_by_player: Optional {player_id: [baseline_dicts]}.
        alert_config:              Optional per-position-group thresholds.

    Returns:
        Package dict with keys: period_name, session_id, clip_count,
        generated_at, pose_data_available, effort_summary,
        formation_summary, identity_conflicts, bio_deviation_alerts,
        all_alerts.
    """
    cfg = alert_config or {}
    log.info(
        "period_package_build",
        period_name=period_name,
        clip_count=len(clips),
        pose_available=pose_metrics is not None,
    )

    # ── Effort anomaly detection ──────────────────────────────────────────────
    sprint_by_player = _aggregate_sprint_by_player(metrics)
    all_session_sprints: list[float] = [
        s for vals in sprint_by_player.values() for s in vals
    ]

    effort_alerts: list[dict[str, Any]] = []
    for player_id, sprints in sprint_by_player.items():
        if not sprints:
            continue
        player_avg_sprint = sum(sprints) / len(sprints)
        pg = _player_position_group(player_id, tracklets)
        pg_cfg = cfg.get(pg, {}).get("effort_anomaly", {})
        threshold_sd = float(pg_cfg.get("deviation_sd", effort_anomaly.DEFAULT_THRESHOLD_SD))
        min_reps = int(pg_cfg.get("min_session_reps", effort_anomaly.DEFAULT_MIN_SESSION_REPS))

        clip_id, clip_uri = _last_clip_for_player(player_id, metrics)
        alerts = effort_anomaly.run(
            player_id=player_id,
            clip_id=clip_id,
            clip_uri=clip_uri,
            position_group=pg,
            sprint_value=player_avg_sprint,
            session_sprints=all_session_sprints,
            threshold_sd=threshold_sd,
            min_session_reps=min_reps,
        )
        effort_alerts.extend(alerts)

    # ── Formation summary ─────────────────────────────────────────────────────
    formation_summary = _build_formation_summary(labels)

    # ── Bio deviation alerts (only when pose data present) ───────────────────
    bio_alerts: list[dict[str, Any]] = []
    if pose_metrics is not None and baseline_metrics_by_player is not None:
        bio_alerts = _check_bio_deviations(
            pose_metrics=pose_metrics,
            baseline_metrics_by_player=baseline_metrics_by_player,
            tracklets=tracklets,
            metrics=metrics,
            alert_config=cfg,
        )

    all_alerts = effort_alerts + bio_alerts

    return {
        "period_name": period_name,
        "session_id": session_id,
        "clip_count": len(clips),
        "generated_at": datetime.now(UTC).isoformat(),
        "pose_data_available": pose_metrics is not None,
        "effort_summary": {
            "player_count": len(sprint_by_player),
            "session_mean_sprint_yps": (
                round(sum(all_session_sprints) / len(all_session_sprints), 3)
                if all_session_sprints
                else None
            ),
            "effort_alerts": effort_alerts,
        },
        "formation_summary": formation_summary,
        "identity_conflicts": identity_conflicts,
        "bio_deviation_alerts": bio_alerts,
        "all_alerts": all_alerts,
    }


# ── Private helpers ────────────────────────────────────────────────────────────


def _aggregate_sprint_by_player(
    metrics: list[dict[str, Any]],
) -> dict[str, list[float]]:
    """Group sprint_to_ball metric values by player_id."""
    result: dict[str, list[float]] = {}
    for m in metrics:
        if m.get("metric_name") != "sprint_to_ball":
            continue
        pid = m.get("player_id") or "unknown"
        val = m.get("metric_value", {})
        sprint_val = val.get("max_speed_yps")
        if sprint_val is not None:
            result.setdefault(pid, []).append(float(sprint_val))
    return result


def _player_position_group(
    player_id: str,
    tracklets: list[dict[str, Any]],
) -> str:
    """Return position group for a player from their tracklets; 'UNKNOWN' if missing."""
    for t in tracklets:
        if t.get("player_id") == player_id:
            pg = t.get("position_group")
            if pg:
                return str(pg).upper()
    return "UNKNOWN"


def _last_clip_for_player(
    player_id: str,
    metrics: list[dict[str, Any]],
) -> tuple[str, str]:
    """Return (clip_id, clip_uri) from the most recent metric for a player."""
    for m in reversed(metrics):
        if m.get("player_id") == player_id:
            return str(m.get("clip_id", "")), str(m.get("evidence_uri") or "")
    return "", ""


def _build_formation_summary(
    labels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate formation / motion labels into a period summary."""
    formation_counts: dict[str, int] = {}
    motion_counts: dict[str, int] = {}

    for label in labels:
        lt = label.get("label_type", "")
        lv = label.get("label_value", {}) if isinstance(label.get("label_value"), dict) else {}
        if lt == "formation":
            name = lv.get("name") or lv.get("formation") or "unknown"
            formation_counts[name] = formation_counts.get(name, 0) + 1
        elif lt == "motion":
            name = lv.get("name") or lv.get("motion") or "unknown"
            motion_counts[name] = motion_counts.get(name, 0) + 1

    return {
        "formation_distribution": formation_counts,
        "motion_distribution": motion_counts,
        "total_labels": len(labels),
    }


def _check_bio_deviations(
    pose_metrics: list[dict[str, Any]],
    baseline_metrics_by_player: dict[str, list[dict[str, Any]]],
    tracklets: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    alert_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run bio deviation checks for all players that have pose + baseline data."""
    bio_alerts: list[dict[str, Any]] = []

    # Group pose metrics by player_id (fall back to tracklet_id)
    pose_by_player: dict[str, list[dict[str, Any]]] = {}
    for pm in pose_metrics:
        pid = pm.get("player_id") or pm.get("tracklet_id") or "unknown"
        pose_by_player.setdefault(str(pid), []).append(pm)

    for player_id, player_pose in pose_by_player.items():
        baseline = baseline_metrics_by_player.get(player_id, [])
        if not baseline:
            continue

        pg = _player_position_group(player_id, tracklets)
        pg_cfg = alert_config.get(pg, {}).get("bio_deviation", {})

        alerts = bio_deviation_alert.run(
            player_id=player_id,
            clip_id=_last_clip_for_player(player_id, metrics)[0],
            clip_uri=_last_clip_for_player(player_id, metrics)[1],
            position_group=pg,
            pose_metrics=player_pose,
            baseline_metrics=baseline,
            pose_pipeline_active=bool(pg_cfg.get("pose_pipeline_active", False)),
            coach_approved=bool(pg_cfg.get("coach_approved", False)),
            deviation_threshold_sd=float(
                pg_cfg.get("deviation_sd", bio_deviation_alert.DEFAULT_DEVIATION_THRESHOLD_SD)
            ),
            min_baseline_samples=int(
                pg_cfg.get("min_baseline_samples", bio_deviation_alert.DEFAULT_MIN_BASELINE_SAMPLES)
            ),
        )
        bio_alerts.extend(alerts)

    return bio_alerts
