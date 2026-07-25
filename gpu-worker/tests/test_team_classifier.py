"""Tests for bounded k=3 CIELab team classification (Issue #130)."""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.stage_labels import run as run_labels
from pipeline.team_classification.kmeans_team_classifier import (
    TEAM_DEFENSE,
    TEAM_OFFENSE,
    TEAM_OFFICIAL,
    KMeansTeamClassifier,
    TrackletColorSample,
    classify_tracklet_frames,
    detect_stripe_score,
    estimate_grass_lab,
)

GRASS = (45, 135, 45)
OFFENSE_RED = (20, 30, 220)
DEFENSE_BLUE = (220, 50, 30)
HELMET_GOLD = (30, 200, 230)
HELMET_WHITE = (240, 240, 240)


def _tracklet(tracklet_id: str, bbox: list[float], frames: range) -> dict[str, Any]:
    return {
        "id": tracklet_id,
        "start_frame": min(frames),
        "end_frame": max(frames),
        "track_points": [
            {
                "frame_number": frame,
                "bbox": bbox,
                "field_x": float(10 + i),
                "field_y": float(i),
            }
            for i, frame in enumerate(frames)
        ],
    }


def _paint_player(
    frame: np.ndarray,
    bbox: list[float],
    jersey_bgr: tuple[int, int, int],
    *,
    helmet_bgr: tuple[int, int, int] = HELMET_WHITE,
    striped: bool = False,
) -> None:
    x1, y1, x2, y2 = [int(v) for v in bbox]
    cv2.rectangle(frame, (x1, y1), (x2, y2), jersey_bgr, thickness=-1)
    cv2.rectangle(frame, (x1 + 6, y1), (x2 - 6, y1 + 14), helmet_bgr, thickness=-1)
    if striped:
        torso_top = y1 + int(0.28 * (y2 - y1))
        torso_bottom = y1 + int(0.72 * (y2 - y1))
        cv2.rectangle(frame, (x1, torso_top), (x2, torso_bottom), (245, 245, 245), -1)
        for offset in range(-40, 80, 10):
            cv2.line(
                frame,
                (x1 + offset, torso_bottom),
                (x1 + offset + 45, torso_top),
                (10, 10, 10),
                thickness=4,
            )


def _frame(
    *,
    red_shift: int = 0,
    blue_shift: int = 0,
) -> np.ndarray:
    frame = np.full((180, 260, 3), GRASS, dtype=np.uint8)
    _paint_player(
        frame,
        [20, 30, 60, 130],
        (20, 30, min(255, 220 + red_shift)),
        helmet_bgr=HELMET_GOLD,
    )
    _paint_player(
        frame,
        [100, 30, 140, 130],
        (min(255, 220 + blue_shift), 50, 30),
        helmet_bgr=HELMET_WHITE,
    )
    _paint_player(
        frame,
        [180, 30, 220, 130],
        (240, 240, 240),
        helmet_bgr=HELMET_WHITE,
        striped=True,
    )
    return frame


def test_classifier_identifies_three_visual_groups() -> None:
    frames = {0: _frame()}
    tracklets = [
        _tracklet("offense", [20, 30, 60, 130], range(0, 1)),
        _tracklet("defense", [100, 30, 140, 130], range(0, 1)),
        _tracklet("official", [180, 30, 220, 130], range(0, 1)),
    ]

    results = classify_tracklet_frames(frames, tracklets, initial_frame=0)

    labels = {
        result.tracklet_id: result.label
        for frame_result in results
        for result in frame_result.results
    }
    assert set(labels.values()) == {TEAM_OFFENSE, TEAM_DEFENSE, TEAM_OFFICIAL}
    assert labels["official"] == TEAM_OFFICIAL


def test_locked_centroids_and_ema_keep_tracklet_labels_stable() -> None:
    frames = {i: _frame(red_shift=min(i, 20), blue_shift=min(i, 12)) for i in range(10)}
    tracklets = [
        _tracklet("offense", [20, 30, 60, 130], range(10)),
        _tracklet("defense", [100, 30, 140, 130], range(10)),
        _tracklet("official", [180, 30, 220, 130], range(10)),
    ]

    results = classify_tracklet_frames(frames, tracklets, initial_frame=0)

    by_tracklet: dict[str, set[str]] = {
        "offense": set(),
        "defense": set(),
        "official": set(),
    }
    for frame_result in results:
        for result in frame_result.results:
            by_tracklet[result.tracklet_id].add(result.label)

    assert all(len(labels) == 1 for labels in by_tracklet.values())
    assert by_tracklet["official"] == {TEAM_OFFICIAL}


def test_helmet_tiebreaker_activates_for_similar_jersey_lab() -> None:
    classifier = KMeansTeamClassifier(helmet_tie_threshold=15.0)
    team_a = TrackletColorSample(
        "a",
        0,
        np.array([100.0, 120.0, 120.0], dtype=np.float32),
        np.array([35.0, 180.0, 150.0], dtype=np.float32),
        0.0,
        100,
    )
    team_b = TrackletColorSample(
        "b",
        0,
        np.array([107.0, 120.0, 120.0], dtype=np.float32),
        np.array([220.0, 120.0, 135.0], dtype=np.float32),
        0.0,
        100,
    )
    official = TrackletColorSample(
        "official",
        0,
        np.array([80.0, 128.0, 128.0], dtype=np.float32),
        np.array([240.0, 128.0, 128.0], dtype=np.float32),
        0.3,
        100,
    )
    grass_lab = np.array([125.0, 90.0, 150.0], dtype=np.float32)
    classifier.fit_initial_frame([team_a, team_b, official], grass_lab)

    assert classifier.centroids is not None
    team_b_cluster = int(
        np.argmin(np.linalg.norm(classifier.centroids - team_b.jersey_lab, axis=1))
    )

    new_sample = TrackletColorSample(
        "new",
        1,
        np.array([103.5, 120.0, 120.0], dtype=np.float32),
        team_b.helmet_lab,
        0.0,
        100,
    )
    result = classifier.classify_frame([new_sample], update=False).results[0]

    assert result.reason == "helmet_tiebreak"
    assert result.cluster_index == team_b_cluster


def test_stripe_detector_scores_official_pattern_above_plain_jersey() -> None:
    plain = np.full((80, 40, 3), OFFENSE_RED, dtype=np.uint8)
    striped_frame = _frame()
    striped_crop = striped_frame[58:102, 180:220]

    assert detect_stripe_score(striped_crop) > detect_stripe_score(plain)
    assert detect_stripe_score(striped_crop) >= 0.18


def test_stage_labels_writes_team_labels_and_patches_tracklets() -> None:
    frames = {0: _frame()}
    tracklets = [
        _tracklet("offense", [20, 30, 60, 130], range(0, 1)),
        _tracklet("defense", [100, 30, 140, 130], range(0, 1)),
        _tracklet("official", [180, 30, 220, 130], range(0, 1)),
    ]
    events = [{"event_type": "snap", "frame_number": 0}]

    created_labels: list[dict[str, Any]] = []
    patched: list[tuple[str, str]] = []

    def fake_create_label(
        label_type: str,
        label_value: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, str]:
        created_labels.append(
            {
                "label_type": label_type,
                "label_value": label_value,
                **kwargs,
            }
        )
        return {"id": f"label-{len(created_labels)}"}

    def fake_patch_tracklet_team_label(
        tracklet_id: str,
        team_label: str,
        **_kwargs: Any,
    ) -> dict[str, str]:
        patched.append((tracklet_id, team_label))
        return {"id": tracklet_id}

    with (
        patch(
            "pipeline.stage_labels.backend.create_label", side_effect=fake_create_label
        ),
        patch(
            "pipeline.stage_labels.backend.patch_tracklet_team_label",
            side_effect=fake_patch_tracklet_team_label,
        ),
    ):
        result = run_labels(
            "clip-1",
            tracklets,
            events,
            fps=30.0,
            frames_by_number=frames,
        )

    team_labels = [
        label
        for label in created_labels
        if label["label_type"] == "team_classification"
    ]
    assert len(team_labels) == 3
    assert ("official", TEAM_OFFICIAL) in patched
    assert result["team_classification"]["classified_tracklets"] == 3


def test_no_frames_falls_back_without_team_classification() -> None:
    result = run_labels("clip-1", [], [], fps=30.0)

    assert result["team_classification"]["classified_tracklets"] == 0
    assert result["team_classification"]["fallback"] == "no_visual_samples"


def test_grass_estimate_uses_green_pixels() -> None:
    frame = np.full((40, 40, 3), GRASS, dtype=np.uint8)
    grass_lab = estimate_grass_lab(frame)

    assert grass_lab.shape == (3,)
    assert float(grass_lab[0]) > 0.0
