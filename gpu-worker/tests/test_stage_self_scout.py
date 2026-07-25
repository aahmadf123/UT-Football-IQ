"""Unit tests for gpu-worker stage_self_scout (Phase 2).

All tests run without GPU hardware, real video footage, or a running backend.
Tests cover:
  - run() with empty clips
  - Formation tendencies (run/pass rates)
  - Motion tendencies (with/without motion)
  - Field zone tendencies
  - Personnel tendencies
  - Down-and-distance tendencies
  - Concept family distribution
  - Pre-snap tell exposure (≥80% lean)
  - Alert generation
  - Evidence clip IDs
"""

import os
import sys

# Add gpu-worker to sys.path so pipeline imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.stage_self_scout import (  # noqa: E402
    _get_concept_family,
    _get_distance_bucket,
    _get_formation,
    _get_motion_detected,
    _get_play_type,
    run,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _clip(clip_id: str, field_zone: str = "mid_field", personnel: str = "11",
          down: int | None = None, distance: float | None = None) -> dict:
    return {
        "id": clip_id,
        "field_zone": field_zone,
        "personnel_grouping": personnel,
        "down": down,
        "distance": distance,
    }


def _label(label_type: str, label_value: dict) -> dict:
    return {"label_type": label_type, "label_value": label_value}


def _run_labels() -> list[dict]:
    return [
        _label("offensive_formation", {"formation": "pistol"}),
        _label("play_concept", {"concept": "inside_zone"}),
    ]


def _pass_labels() -> list[dict]:
    return [
        _label("offensive_formation", {"formation": "shotgun"}),
        _label("play_concept", {"concept": "pass"}),
    ]


# ── Helper function tests ────────────────────────────────────────────────────


class TestGetPlayType:
    def test_run_from_concept(self) -> None:
        labels = [_label("play_concept", {"concept": "inside_zone"})]
        assert _get_play_type(labels) == "run"

    def test_pass_from_concept(self) -> None:
        labels = [_label("play_concept", {"concept": "pass"})]
        assert _get_play_type(labels) == "pass"

    def test_run_from_direction(self) -> None:
        labels = [_label("play_direction", {"direction": "left"})]
        assert _get_play_type(labels) == "run"

    def test_none_for_unknown(self) -> None:
        labels = [_label("coverage", {"type": "cover3"})]
        assert _get_play_type(labels) is None


class TestGetFormation:
    def test_returns_formation(self) -> None:
        labels = [_label("offensive_formation", {"formation": "shotgun"})]
        assert _get_formation(labels) == "shotgun"

    def test_none_when_missing(self) -> None:
        labels = [_label("play_concept", {"concept": "pass"})]
        assert _get_formation(labels) is None


class TestGetMotionDetected:
    def test_motion_true(self) -> None:
        labels = [_label("motion_detected", {"detected": True})]
        assert _get_motion_detected(labels) is True

    def test_motion_false(self) -> None:
        labels = [_label("motion_detected", {"detected": False})]
        assert _get_motion_detected(labels) is False

    def test_no_motion_label(self) -> None:
        labels = [_label("play_concept", {"concept": "pass"})]
        assert _get_motion_detected(labels) is False


class TestGetConceptFamily:
    def test_inside_zone(self) -> None:
        labels = [_label("play_concept", {"concept": "inside_zone"})]
        assert _get_concept_family(labels) == "inside_zone"

    def test_outside_zone(self) -> None:
        labels = [_label("play_concept", {"concept": "outside_zone"})]
        assert _get_concept_family(labels) == "outside_zone"

    def test_gap(self) -> None:
        labels = [_label("play_concept", {"concept": "power"})]
        assert _get_concept_family(labels) == "gap"

    def test_pass_concept(self) -> None:
        labels = [_label("play_concept", {"concept": "screen"})]
        assert _get_concept_family(labels) == "pass_concept"

    def test_none_for_unknown(self) -> None:
        labels = [_label("coverage", {"type": "cover3"})]
        assert _get_concept_family(labels) is None


class TestGetDistanceBucket:
    def test_short(self) -> None:
        assert _get_distance_bucket(2.0) == "short"

    def test_medium(self) -> None:
        assert _get_distance_bucket(5.0) == "medium"

    def test_long(self) -> None:
        assert _get_distance_bucket(10.0) == "long"

    def test_none(self) -> None:
        assert _get_distance_bucket(None) is None


# ── Integration tests ────────────────────────────────────────────────────────


class TestRunEmpty:
    def test_empty_clips_returns_structure(self) -> None:
        result = run([], {}, {})
        assert result["clip_count"] == 0
        assert result["formation_tendencies"] == []
        assert result["motion_tendencies"]["with_motion"]["total"] == 0
        assert result["field_zone_tendencies"] == []
        assert result["personnel_tendencies"] == []
        assert result["down_distance_tendencies"] == []
        assert result["formation_concept_families"] == {}
        assert result["pre_snap_tells"] == []
        assert result["alerts"] == []


class TestFormationTendencies:
    def test_basic_formation_counts(self) -> None:
        clips = [_clip(f"c{i}") for i in range(6)]
        labels_by_clip = {
            "c0": _run_labels(),
            "c1": _run_labels(),
            "c2": _run_labels(),
            "c3": _pass_labels(),
            "c4": _pass_labels(),
            "c5": _pass_labels(),
        }
        result = run(clips, labels_by_clip, {})
        formations = {ft["formation"]: ft for ft in result["formation_tendencies"]}
        assert "pistol" in formations
        assert formations["pistol"]["total_plays"] == 3
        assert formations["pistol"]["run_rate"] == 1.0
        assert "shotgun" in formations
        assert formations["shotgun"]["pass_rate"] == 1.0


class TestMotionTendencies:
    def test_motion_split(self) -> None:
        clips = [_clip(f"c{i}") for i in range(4)]
        labels_by_clip = {
            "c0": [_label("play_concept", {"concept": "run"}), _label("motion_detected", {"detected": True})],
            "c1": [_label("play_concept", {"concept": "pass"}), _label("motion_detected", {"detected": True})],
            "c2": [_label("play_concept", {"concept": "run"})],
            "c3": [_label("play_concept", {"concept": "pass"})],
        }
        result = run(clips, labels_by_clip, {})
        with_m = result["motion_tendencies"]["with_motion"]
        without_m = result["motion_tendencies"]["without_motion"]
        assert with_m["total"] == 2
        assert without_m["total"] == 2


class TestDownDistanceTendencies:
    def test_dd_breakdown(self) -> None:
        clips = [
            _clip("c0", down=1, distance=10.0),
            _clip("c1", down=1, distance=10.0),
            _clip("c2", down=2, distance=5.0),
        ]
        labels_by_clip = {
            "c0": [_label("play_concept", {"concept": "run"})],
            "c1": [_label("play_concept", {"concept": "pass"})],
            "c2": [_label("play_concept", {"concept": "pass"})],
        }
        result = run(clips, labels_by_clip, {})
        dd = result["down_distance_tendencies"]
        assert len(dd) >= 1
        first_long = [d for d in dd if d["down"] == 1 and d["distance_bucket"] == "long"]
        assert len(first_long) == 1
        assert first_long[0]["total_plays"] == 2


class TestPreSnapTells:
    def test_high_lean_detected(self) -> None:
        # 5 run plays from pistol without motion → 100% run lean
        clips = [_clip(f"c{i}") for i in range(5)]
        labels_by_clip = {
            f"c{i}": [
                _label("offensive_formation", {"formation": "pistol"}),
                _label("play_concept", {"concept": "power"}),
            ]
            for i in range(5)
        }
        result = run(clips, labels_by_clip, {})
        tells = result["pre_snap_tells"]
        assert len(tells) >= 1
        assert tells[0]["lean"] == "run"
        assert tells[0]["formation"] == "pistol"


class TestAlerts:
    def test_formation_alert_generated(self) -> None:
        # 5 run plays from pistol → high run tendency alert
        clips = [_clip(f"c{i}") for i in range(5)]
        labels_by_clip = {
            f"c{i}": [
                _label("offensive_formation", {"formation": "pistol"}),
                _label("play_concept", {"concept": "power"}),
            ]
            for i in range(5)
        }
        result = run(clips, labels_by_clip, {})
        alerts = result["alerts"]
        formation_alerts = [a for a in alerts if a["alert_type"] == "formation_tendency"]
        assert len(formation_alerts) >= 1


class TestEvidenceClipIds:
    def test_evidence_clip_ids_present(self) -> None:
        clips = [_clip(f"c{i}") for i in range(4)]
        labels_by_clip = {
            f"c{i}": [
                _label("offensive_formation", {"formation": "shotgun"}),
                _label("play_concept", {"concept": "pass"}),
            ]
            for i in range(4)
        }
        result = run(clips, labels_by_clip, {})
        for ft in result["formation_tendencies"]:
            assert "evidence_clip_ids" in ft
            assert isinstance(ft["evidence_clip_ids"], list)

    def test_evidence_clip_ids_limited(self) -> None:
        clips = [_clip(f"c{i}") for i in range(15)]
        labels_by_clip = {
            f"c{i}": [
                _label("offensive_formation", {"formation": "shotgun"}),
                _label("play_concept", {"concept": "pass"}),
            ]
            for i in range(15)
        }
        result = run(clips, labels_by_clip, {})
        for ft in result["formation_tendencies"]:
            assert len(ft["evidence_clip_ids"]) <= 10
