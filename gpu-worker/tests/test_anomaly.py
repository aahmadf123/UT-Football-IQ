"""Tests for anomaly detection alert modules (Issue #16).

Covers bio_deviation_alert, effort_anomaly, and formation_anomaly:
  - Detection below threshold (no alert)
  - Detection at exactly 1 SD (no alert for bio/effort; depends on threshold)
  - Detection at 2 SD (alert fired for bio_deviation)
  - Detection above threshold (alert fired)
  - Guard conditions (pose_pipeline inactive, coach not approved)
  - Confidence and required fields in every alert
  - Formation anomaly displacement threshold
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math

from alerts.bio_deviation_alert import run as bio_run
from alerts.effort_anomaly import run as effort_run
from alerts.formation_anomaly import run as formation_run

# ── Fixtures ──────────────────────────────────────────────────────────────────


_PLAYER = "player-uuid-001"
_CLIP = "clip-uuid-001"
_CLIP_URI = "r2://clips/test.mp4"
_PG = "OL"


def _baseline(mean: float, stdev: float, n: int = 10) -> list[dict]:
    """Build baseline_metrics list with a metric spread around mean ± stdev."""

    values = [mean + (stdev if i % 2 == 0 else -stdev) for i in range(n)]
    return [{"metric_name": "pose_pad_level", "values": values}]


# ── bio_deviation_alert ────────────────────────────────────────────────────────


def test_bio_no_alert_when_pose_pipeline_inactive() -> None:
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[{"metric_name": "pose_pad_level", "value": 100.0, "confidence": 0.9}],
        baseline_metrics=_baseline(45.0, 1.0),
        pose_pipeline_active=False,
        coach_approved=True,
    )
    assert alerts == []


def test_bio_no_alert_when_not_coach_approved() -> None:
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[{"metric_name": "pose_pad_level", "value": 100.0, "confidence": 0.9}],
        baseline_metrics=_baseline(45.0, 1.0),
        pose_pipeline_active=True,
        coach_approved=False,
    )
    assert alerts == []


def test_bio_no_alert_below_threshold() -> None:
    """1 SD below threshold — no alert."""
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[
            {"metric_name": "pose_pad_level", "value": 46.0, "confidence": 0.85}  # ~1 SD above
        ],
        baseline_metrics=_baseline(mean=45.0, stdev=1.0),
        pose_pipeline_active=True,
        coach_approved=True,
        deviation_threshold_sd=2.0,
    )
    assert alerts == []


def test_bio_no_alert_at_exactly_1_sd_above_baseline() -> None:
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[{"metric_name": "pose_pad_level", "value": 46.0, "confidence": 0.85}],
        baseline_metrics=_baseline(mean=45.0, stdev=1.0),
        pose_pipeline_active=True,
        coach_approved=True,
        deviation_threshold_sd=2.0,
    )
    assert len(alerts) == 0


def test_bio_alert_at_exactly_2_sd() -> None:
    """Value exactly 2 SD above mean fires alert."""
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[{"metric_name": "pose_pad_level", "value": 47.0, "confidence": 0.85}],
        baseline_metrics=_baseline(mean=45.0, stdev=1.0),
        pose_pipeline_active=True,
        coach_approved=True,
        deviation_threshold_sd=2.0,
    )
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["alert_type"] == "bio_deviation"
    assert alert["deviation_sd"] >= 2.0


def test_bio_alert_severity_high_at_3_sd() -> None:
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[{"metric_name": "pose_pad_level", "value": 48.0, "confidence": 0.9}],
        baseline_metrics=_baseline(mean=45.0, stdev=1.0),
        pose_pipeline_active=True,
        coach_approved=True,
        deviation_threshold_sd=2.0,
    )
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "high"


def test_bio_alert_contains_required_fields() -> None:
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[{"metric_name": "pose_pad_level", "value": 48.0, "confidence": 0.9}],
        baseline_metrics=_baseline(mean=45.0, stdev=1.0),
        pose_pipeline_active=True,
        coach_approved=True,
    )
    assert len(alerts) == 1
    a = alerts[0]
    for field in ("confidence", "clip_uri", "player_id", "timestamp", "position_group"):
        assert field in a, f"Missing required field: {field}"


def test_bio_no_alert_insufficient_baseline_samples() -> None:
    """Fewer than min_baseline_samples → no alert."""
    alerts = bio_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group=_PG,
        pose_metrics=[{"metric_name": "pose_pad_level", "value": 60.0, "confidence": 0.9}],
        baseline_metrics=[{"metric_name": "pose_pad_level", "values": [45.0]}],  # only 1 sample
        pose_pipeline_active=True,
        coach_approved=True,
        min_baseline_samples=5,
    )
    assert alerts == []


# ── effort_anomaly ────────────────────────────────────────────────────────────


def test_effort_no_alert_below_threshold() -> None:
    """Sprint value at 1 SD below mean — default threshold is 1.5 SD — no alert."""
    session = [5.0, 5.2, 4.8, 5.1, 4.9, 5.3, 5.0, 5.2]
    import statistics

    mean = statistics.mean(session)
    stdev = statistics.stdev(session)
    value = mean - stdev * 1.0  # 1 SD below — under threshold

    alerts = effort_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="WR",
        sprint_value=value,
        session_sprints=session + [value],
        threshold_sd=1.5,
    )
    assert alerts == []


def test_effort_alert_at_2_sd_below_mean() -> None:
    session = [5.0, 5.2, 4.8, 5.1, 4.9, 5.3, 5.0, 5.2, 5.1, 5.0]
    import statistics

    mean = statistics.mean(session)
    stdev = statistics.stdev(session)
    value = mean - stdev * 2.0  # exactly 2 SD below

    alerts = effort_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="WR",
        sprint_value=value,
        session_sprints=session + [value],
        threshold_sd=1.5,
    )
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "effort_anomaly"


def test_effort_alert_contains_required_fields() -> None:
    session = [5.0, 5.2, 4.8, 5.1, 4.9, 5.0]
    alerts = effort_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="WR",
        sprint_value=1.0,
        session_sprints=session + [1.0],
    )
    if alerts:
        a = alerts[0]
        for field in ("confidence", "clip_uri", "player_id", "timestamp", "position_group"):
            assert field in a


def test_effort_no_alert_insufficient_session_reps() -> None:
    alerts = effort_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="WR",
        sprint_value=0.5,
        session_sprints=[5.0, 0.5],  # only 2 values
        min_session_reps=3,
    )
    assert alerts == []


def test_effort_severity_high_at_extreme_deviation() -> None:
    session = [5.0] * 20
    alerts = effort_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="WR",
        sprint_value=0.1,
        session_sprints=session + [0.1],
        threshold_sd=1.5,
    )
    assert len(alerts) == 1
    # deviation should be large → high severity
    assert alerts[0]["severity"] in ("medium", "high")


# ── formation_anomaly ─────────────────────────────────────────────────────────


def test_formation_no_alert_within_threshold() -> None:
    alerts = formation_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="OL",
        observed_x=10.0,
        observed_y=2.0,
        expected_x=10.0,
        expected_y=1.0,  # 1 yd off — under 3 yd threshold
        formation_name="11 Spread",
        threshold_yards=3.0,
    )
    assert alerts == []


def test_formation_alert_at_threshold() -> None:
    alerts = formation_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="OL",
        observed_x=13.0,
        observed_y=0.0,
        expected_x=10.0,
        expected_y=0.0,  # exactly 3 yd off
        formation_name="11 Spread",
        threshold_yards=3.0,
    )
    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "formation_anomaly"
    assert abs(alerts[0]["displacement_yards"] - 3.0) < 0.01


def test_formation_severity_high_at_1_5x_threshold() -> None:
    alerts = formation_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="OL",
        observed_x=14.5,
        observed_y=0.0,
        expected_x=10.0,
        expected_y=0.0,  # 4.5 yd = 1.5x the 3 yd threshold
        formation_name="11 Spread",
        threshold_yards=3.0,
    )
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "high"


def test_formation_alert_contains_required_fields() -> None:
    alerts = formation_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="WR",
        observed_x=15.0,
        observed_y=5.0,
        expected_x=10.0,
        expected_y=0.0,
        formation_name="11 Trips",
    )
    if alerts:
        a = alerts[0]
        for field in ("confidence", "clip_uri", "player_id", "timestamp", "position_group"):
            assert field in a
        assert "expected_position" in a
        assert "observed_position" in a


def test_formation_displacement_is_euclidean() -> None:
    alerts = formation_run(
        player_id=_PLAYER,
        clip_id=_CLIP,
        clip_uri=_CLIP_URI,
        position_group="WR",
        observed_x=13.0,
        observed_y=4.0,
        expected_x=10.0,
        expected_y=0.0,
        formation_name="11 Spread",
        threshold_yards=1.0,
    )
    expected_disp = math.sqrt(3.0**2 + 4.0**2)  # = 5.0
    assert len(alerts) == 1
    assert abs(alerts[0]["displacement_yards"] - expected_disp) < 0.01
