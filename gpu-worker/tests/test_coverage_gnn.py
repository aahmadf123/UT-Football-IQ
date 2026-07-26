"""Tests for the coverage classifier + uncertainty + bust flag (Issue #139)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.coverage import train_coverage_gnn as trainer
from pipeline.coverage.gnn_classifier import (
    COVERAGE_CLASSES,
    CoverageClassifier,
    CoverageGNN,
    DeterministicCoverageBaseline,
    coverage_bust_flag,
    graph_embedding,
)
from pipeline.spatial import feature_schema as fs

LOS = 50.0


def _defender(tid: str, x: float, y: float, *, frames: tuple[int, ...] = (10,)) -> dict[str, Any]:
    return {
        "id": tid,
        "side_of_ball": "defense",
        "track_points": [
            {"frame_number": f, "field_x": x, "field_y": y} for f in frames
        ],
    }


def _defense_look(deep: int, box: int) -> list[dict[str, Any]]:
    """Build a defensive look: ``deep`` safeties downfield + ``box`` defenders."""
    tracks: list[dict[str, Any]] = []
    for i in range(deep):
        y = -12.0 + 24.0 * (i / max(deep - 1, 1)) if deep > 1 else 0.0
        tracks.append(_defender(f"deep{i}", LOS + 15.0, y))
    for i in range(box):
        y = -8.0 + 16.0 * (i / max(box - 1, 1)) if box > 1 else 0.0
        tracks.append(_defender(f"box{i}", LOS + 3.0, y))
    return tracks


def _nodes_and_graph(tracks: list[dict[str, Any]]) -> tuple[list[fs.PlayerNode], fs.SpatialGraph]:
    nodes = fs.build_player_nodes(tracks, 10, LOS, calibration_confirmed=True)
    return nodes, fs.build_spatial_graph(nodes)


def test_taxonomy_has_nine_classes() -> None:
    assert len(COVERAGE_CLASSES) == 9
    assert "cover_2_mof" in COVERAGE_CLASSES
    assert "cover_2_shell" in COVERAGE_CLASSES
    assert "bracket_match" in COVERAGE_CLASSES


def test_baseline_returns_valid_distribution() -> None:
    baseline = DeterministicCoverageBaseline()
    nodes, _ = _nodes_and_graph(_defense_look(deep=3, box=6))
    probs = baseline.predict_proba(nodes)
    assert set(probs) == set(COVERAGE_CLASSES)
    assert sum(probs.values()) == pytest.approx(1.0)
    # 3 deep safeties → cover_3 most likely.
    assert max(probs, key=lambda k: probs[k]) == "cover_3"


def test_baseline_zero_deep_is_cover_0() -> None:
    baseline = DeterministicCoverageBaseline()
    nodes, _ = _nodes_and_graph(_defense_look(deep=0, box=7))
    probs = baseline.predict_proba(nodes)
    assert max(probs, key=lambda k: probs[k]) == "cover_0"


def test_classifier_default_path_is_uncalibrated_experimental() -> None:
    clf = CoverageClassifier(baseline=DeterministicCoverageBaseline())  # no GNN
    nodes, graph = _nodes_and_graph(_defense_look(deep=2, box=5))
    out = clf.classify(nodes, graph)
    assert out.value in COVERAGE_CLASSES
    assert out.is_calibrated is False
    assert out.experimental is True
    assert out.calibration_method == "uncalibrated"
    assert 0.0 <= out.confidence <= 1.0
    assert out.detail["model"] == "coverage-baseline-heuristic"
    assert out.detail["deep_safeties"] == 2


def test_graph_embedding_dimension() -> None:
    _, graph = _nodes_and_graph(_defense_look(deep=2, box=5))
    emb = graph_embedding(graph)
    assert emb.shape == (4 * len(fs.NODE_FEATURE_NAMES),)


def test_coverage_gnn_fit_predict_and_roundtrip(tmp_path: Path) -> None:
    # Two separable looks → learnable head should fit them.
    graphs: list[fs.SpatialGraph] = []
    labels: list[int] = []
    for _ in range(8):
        graphs.append(_nodes_and_graph(_defense_look(deep=0, box=7))[1])
        labels.append(COVERAGE_CLASSES.index("cover_0"))
        graphs.append(_nodes_and_graph(_defense_look(deep=4, box=6))[1])
        labels.append(COVERAGE_CLASSES.index("cover_4"))

    gnn, report = trainer.train(graphs, labels, holdout_frac=0.25, seed=1)
    assert gnn.is_loaded
    assert report.train_accuracy >= 0.75

    probs = gnn.predict_proba(_nodes_and_graph(_defense_look(deep=4, box=6))[1])
    assert sum(probs.values()) == pytest.approx(1.0)

    # Checkpoint round-trips and is loadable via env (no hard-coded path).
    ckpt = tmp_path / "coverage.json"
    trainer.save_checkpoint(gnn, ckpt, report=report, source="unit-test-synthetic")
    payload = json.loads(ckpt.read_text())
    assert payload["provenance"]["usage"] == trainer.USAGE_MARKER

    restored = CoverageGNN.from_checkpoint(payload)
    assert restored.is_loaded
    restored_probs = restored.predict_proba(graphs[0])
    assert set(restored_probs) == set(COVERAGE_CLASSES)
    assert sum(restored_probs.values()) == pytest.approx(1.0)


def test_classifier_uses_loaded_gnn(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    graphs: list[fs.SpatialGraph] = []
    labels: list[int] = []
    for _ in range(8):
        graphs.append(_nodes_and_graph(_defense_look(deep=0, box=7))[1])
        labels.append(COVERAGE_CLASSES.index("cover_0"))
        graphs.append(_nodes_and_graph(_defense_look(deep=4, box=6))[1])
        labels.append(COVERAGE_CLASSES.index("cover_4"))
    gnn, report = trainer.train(graphs, labels, holdout_frac=0.25, seed=2)
    ckpt = tmp_path / "coverage.json"
    trainer.save_checkpoint(gnn, ckpt, report=report, source="unit-test-synthetic")

    monkeypatch.setenv("COVERAGE_GNN_MODEL", str(ckpt))
    clf = CoverageClassifier.from_env()
    assert clf.gnn is not None and clf.gnn.is_loaded
    nodes, graph = _nodes_and_graph(_defense_look(deep=4, box=6))
    out = clf.classify(nodes, graph)
    assert out.detail["model"] == "coverage-gnn-numpy"
    # Calibrated because train() fit a temperature scaler on the holdout.
    assert out.calibration_method == "temperature"
    assert out.is_calibrated is True


def test_missing_checkpoint_falls_back_to_baseline(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("COVERAGE_GNN_MODEL", "/nonexistent/coverage.json")
    clf = CoverageClassifier.from_env()
    assert clf.gnn is None  # missing file → no crash, falls back
    nodes, graph = _nodes_and_graph(_defense_look(deep=3, box=6))
    out = clf.classify(nodes, graph)
    assert out.detail["model"] == "coverage-baseline-heuristic"


def test_coverage_bust_flag_fires_on_rapid_divergence() -> None:
    # A defender that jumps 5 yd within the 0.3 s window → bust.
    busted = {
        "id": "rotator",
        "side_of_ball": "defense",
        "track_points": [
            {"frame_number": 10, "field_x": 60.0, "field_y": 0.0},
            {"frame_number": 16, "field_x": 60.0, "field_y": 5.0},
        ],
    }
    stable = _defender("still", 62.0, 8.0, frames=(10, 16))
    result = coverage_bust_flag([busted, stable], snap_frame=10, fps=20.0)
    assert result["coverage_bust_flag"] is True
    assert result["busted_track_id"] == "rotator"
    assert result["max_displacement_yards"] >= 5.0


def test_coverage_bust_flag_quiet_when_aligned() -> None:
    tracks = [_defender(f"d{i}", 60.0, float(i), frames=(10, 16)) for i in range(4)]
    result = coverage_bust_flag(tracks, snap_frame=10, fps=20.0)
    assert result["coverage_bust_flag"] is False
    assert result["busted_track_id"] is None


# ── Stage integration ─────────────────────────────────────────────────────────


def _offense_skill() -> list[dict[str, Any]]:
    return [
        {
            "id": f"o{i}",
            "side_of_ball": "offense",
            "track_points": [{"frame_number": 10, "field_x": LOS - 1.0, "field_y": y}],
        }
        for i, y in enumerate((-18.0, -6.0, 6.0, 18.0))
    ]


def test_stage_coverage_calibrated_path(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pipeline import stage_coverage

    # Offline: backend writes are swallowed; we assert on the returned shell.
    tracks = _defense_look(deep=3, box=6) + _offense_skill()
    events = [{"event_type": "snap", "frame_number": 10}]
    result = stage_coverage.run(
        "clip-1", tracks, events, fps=30.0, analytics_safe=True, offense_direction=1
    )
    assert result["shell"] in COVERAGE_CLASSES
    assert 0.0 <= result["coverage_confidence"] <= 1.0
    assert result["calibration_method"] == "uncalibrated"  # no checkpoint loaded
    assert "coverage_bust_flag" in result


def test_stage_coverage_unresolved_orientation_takes_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """analytics_safe alone must not reach the graph path.

    depth_behind_los is signed by the attacking direction; without it the graph
    would be built with every defender's depth mirrored, so the stage has to
    take the same route it takes for unconfirmed coordinates.
    """
    from pipeline import stage_coverage
    from pipeline.spatial import feature_schema as fs

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("graph path must not run without an orientation")

    monkeypatch.setattr(fs, "build_player_nodes", _fail)
    tracks = _defense_look(deep=3, box=6) + _offense_skill()
    events = [{"event_type": "snap", "frame_number": 10}]
    result = stage_coverage.run(
        "clip-1", tracks, events, fps=30.0, analytics_safe=True, offense_direction=None
    )
    assert result["calibration_method"] == "uncalibrated"
    assert result["is_calibrated"] is False


def test_stage_coverage_uncalibrated_coords_fallback() -> None:
    from pipeline import stage_coverage

    tracks = _defense_look(deep=2, box=5) + _offense_skill()
    events = [{"event_type": "snap", "frame_number": 10}]
    # analytics_safe False → legacy heuristic, flagged uncalibrated coords.
    result = stage_coverage.run("clip-1", tracks, events, fps=30.0, analytics_safe=False)
    assert result["calibration_method"] == "uncalibrated"
    assert result["shell"] is not None
    assert result["coverage_bust_flag"] is False
