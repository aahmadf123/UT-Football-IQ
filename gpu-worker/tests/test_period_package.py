"""Tests for gpu-worker/pipeline/period_package.py (Issue #16).

Covers:
  - build() with no pose data (pose_data_available=False in output)
  - build() with pose data present (pose_data_available=True)
  - effort_summary populated from sprint_to_ball metrics
  - formation_summary aggregates labels correctly
  - identity_conflicts passed through unchanged
  - bio_deviation_alerts empty when no coach approval (default config)
  - bio_deviation_alerts present when config has pose_pipeline_active+coach_approved
"""

import os
import sys

import pytest

# Allow imports from gpu-worker root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.period_package import (
    _aggregate_sprint_by_player,
    _build_formation_summary,
    _player_position_group,
    build,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _sprint_metric(player_id: str, speed: float, clip_id: str = "clip-1") -> dict:
    return {
        "metric_name": "sprint_to_ball",
        "player_id": player_id,
        "clip_id": clip_id,
        "metric_value": {"max_speed_yps": speed, "confidence": 0.7},
        "evidence_uri": f"r2://clips/{clip_id}.mp4",
    }


def _formation_label(formation: str) -> dict:
    return {"label_type": "formation", "label_value": {"name": formation}}


def _motion_label(motion: str) -> dict:
    return {"label_type": "motion", "label_value": {"name": motion}}


def _tracklet(player_id: str, pg: str) -> dict:
    return {
        "player_id": player_id,
        "position_group": pg,
        "start_frame": 0,
        "end_frame": 100,
    }


# ── Pure helper unit tests ────────────────────────────────────────────────────


def test_aggregate_sprint_by_player_groups_correctly() -> None:
    metrics = [
        _sprint_metric("p1", 5.0),
        _sprint_metric("p1", 4.5),
        _sprint_metric("p2", 6.1),
    ]
    result = _aggregate_sprint_by_player(metrics)
    assert "p1" in result
    assert len(result["p1"]) == 2
    assert result["p2"] == [6.1]


def test_aggregate_sprint_ignores_other_metric_names() -> None:
    metrics = [
        {"metric_name": "max_speed", "player_id": "p1", "metric_value": {"yards_per_second": 5.0}},
        _sprint_metric("p2", 4.0),
    ]
    result = _aggregate_sprint_by_player(metrics)
    assert "p1" not in result
    assert "p2" in result


def test_build_formation_summary_counts_correctly() -> None:
    labels = [
        _formation_label("11 Spread"),
        _formation_label("11 Spread"),
        _formation_label("12 Pro"),
        _motion_label("jet"),
    ]
    summary = _build_formation_summary(labels)
    assert summary["formation_distribution"]["11 Spread"] == 2
    assert summary["formation_distribution"]["12 Pro"] == 1
    assert summary["motion_distribution"]["jet"] == 1
    assert summary["total_labels"] == 4


def test_player_position_group_found() -> None:
    tracklets = [_tracklet("p1", "OL"), _tracklet("p2", "WR")]
    assert _player_position_group("p1", tracklets) == "OL"
    assert _player_position_group("p2", tracklets) == "WR"


def test_player_position_group_missing_returns_unknown() -> None:
    assert _player_position_group("p99", []) == "UNKNOWN"


# ── build() integration tests ─────────────────────────────────────────────────


def test_build_no_pose_data() -> None:
    package = build(
        period_name="Period 1",
        session_id="sess-001",
        clips=[{"id": "clip-1"}],
        tracklets=[_tracklet("p1", "OL")],
        metrics=[_sprint_metric("p1", 4.0)],
        labels=[_formation_label("11 Spread")],
        identity_conflicts=[],
        pose_metrics=None,
        baseline_metrics_by_player=None,
    )
    assert package["pose_data_available"] is False
    assert package["bio_deviation_alerts"] == []
    assert package["period_name"] == "Period 1"
    assert package["clip_count"] == 1


def test_build_with_pose_data_no_conflicts() -> None:
    package = build(
        period_name="Period 2",
        session_id="sess-001",
        clips=[{"id": "clip-2"}],
        tracklets=[_tracklet("p1", "OL")],
        metrics=[_sprint_metric("p1", 4.0, "clip-2")],
        labels=[],
        identity_conflicts=[],
        pose_metrics=[
            {
                "metric_name": "pose_pad_level",
                "player_id": "p1",
                "value": 45.0,
                "confidence": 0.88,
            }
        ],
        baseline_metrics_by_player={
            "p1": [
                {
                    "metric_name": "pose_pad_level",
                    "values": [44.0, 45.0, 46.0, 44.5, 45.5, 44.8],
                }
            ]
        },
        alert_config={},  # no coach_approved → bio alerts suppressed
    )
    assert package["pose_data_available"] is True
    assert package["bio_deviation_alerts"] == []


def test_build_with_pose_data_and_approved_config() -> None:
    """When pose_pipeline_active + coach_approved, a 3 SD deviation triggers alert."""
    package = build(
        period_name="Period 3",
        session_id="sess-002",
        clips=[{"id": "clip-3"}],
        tracklets=[_tracklet("p1", "OL")],
        metrics=[_sprint_metric("p1", 4.0, "clip-3")],
        labels=[],
        identity_conflicts=[],
        pose_metrics=[
            {
                "metric_name": "pose_pad_level",
                "player_id": "p1",
                "value": 80.0,  # far above baseline
                "confidence": 0.9,
            }
        ],
        baseline_metrics_by_player={
            "p1": [
                {
                    "metric_name": "pose_pad_level",
                    "values": [44.0, 45.0, 44.5, 43.8, 44.2, 45.1, 44.7, 45.3],
                }
            ]
        },
        alert_config={
            "OL": {
                "bio_deviation": {
                    "pose_pipeline_active": True,
                    "coach_approved": True,
                    "deviation_sd": 2.0,
                }
            }
        },
    )
    assert package["pose_data_available"] is True
    assert len(package["bio_deviation_alerts"]) >= 1
    bio_alert = package["bio_deviation_alerts"][0]
    assert bio_alert["alert_type"] == "bio_deviation"
    assert bio_alert["player_id"] == "p1"
    assert bio_alert["position_group"] == "OL"


def test_build_effort_anomaly_detected() -> None:
    """A player 2 SD below session mean triggers an effort alert."""
    sprints = [5.0, 5.2, 4.8, 5.1, 4.9, 5.0, 5.3, 1.0]  # last is anomalous
    metrics = [_sprint_metric(f"p{i}", v, f"clip-{i}") for i, v in enumerate(sprints)]
    tracklets = [_tracklet(f"p{i}", "WR") for i in range(len(sprints))]

    package = build(
        period_name="Period 1",
        session_id="sess-003",
        clips=[{"id": f"clip-{i}"} for i in range(len(sprints))],
        tracklets=tracklets,
        metrics=metrics,
        labels=[],
        identity_conflicts=[],
    )
    effort_alerts = package["effort_summary"]["effort_alerts"]
    # The player with sprint 1.0 should be flagged
    assert any(a["metric_value"] == pytest.approx(1.0, abs=0.01) for a in effort_alerts)


def test_build_identity_conflicts_passed_through() -> None:
    conflicts = [{"tracklet_id": "t1", "conflict_type": "jersey_swap"}]
    package = build(
        period_name="Period 1",
        session_id="s1",
        clips=[],
        tracklets=[],
        metrics=[],
        labels=[],
        identity_conflicts=conflicts,
    )
    assert package["identity_conflicts"] == conflicts


def test_build_all_alerts_combined() -> None:
    """all_alerts = effort_alerts + bio_deviation_alerts."""
    package = build(
        period_name="Period 1",
        session_id="s1",
        clips=[{"id": "c1"}],
        tracklets=[_tracklet("p1", "WR")],
        metrics=[_sprint_metric("p1", 0.5, "c1"), _sprint_metric("p2", 5.0, "c1")],
        labels=[],
        identity_conflicts=[],
        pose_metrics=None,
    )
    assert len(package["all_alerts"]) == len(package["effort_summary"]["effort_alerts"])
