"""Optional advanced trackers receive measured pixels, not empty feature hooks."""

from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from pipeline.stage_track import run
from pipeline.tracking.botsort_adapter import BoTSORTTracker
from pipeline.tracking.frame_features import torso_histogram, tracking_frame_features
from pipeline.video_timeline import VideoTimeline
from tests.fake_capture import FakeCapture


def test_torso_histogram_measures_actual_uniform_color() -> None:
    red = np.full((60, 40, 3), (10, 10, 220), dtype=np.uint8)
    blue = np.full((60, 40, 3), (220, 10, 10), dtype=np.uint8)
    red_feature = np.asarray(torso_histogram(red, [0, 0, 40, 60]))
    blue_feature = np.asarray(torso_histogram(blue, [0, 0, 40, 60]))
    assert np.linalg.norm(red_feature) == pytest.approx(1)
    assert red_feature @ blue_feature == 0
    assert torso_histogram(red, [0, 0, 0, 0]) is None


def test_camera_features_recover_measured_pan_and_reset_at_shots() -> None:
    rng = np.random.default_rng(2026)
    # Synthetic textured grass is a test fixture, never an inference fallback.
    source = np.zeros((180, 320, 3), dtype=np.uint8)
    source[:, :, 1] = rng.integers(40, 180, size=(180, 320), dtype=np.uint8)
    following = cv2.warpAffine(source, np.float32([[1, 0, 5], [0, 1, 2]]), (320, 180))
    timeline = VideoTimeline((0, 1/30, 2/30, 0.1), 0.133, 30)
    detections = {"0": [{"bbox": [30, 40, 55, 100], "shot_id": 1}],
                  "3": [{"bbox": [35, 42, 60, 102], "shot_id": 1}]}
    with patch("pipeline.hwaccel.nvdec_video_capture", return_value=FakeCapture(4)), patch(
        "pipeline.tracking.frame_features.iter_frames", return_value=iter([(0, source), (3, following)])
    ):
        enriched, warps, diagnostics = tracking_frame_features(Path("synthetic-test.mp4"), detections, 30, timeline)
    assert diagnostics["appearance_observations"] == 2 and diagnostics["validated"] is False
    assert "embedding" in enriched["0"][0] and "embedding" not in detections["0"][0]
    assert diagnostics["camera_pairs_measured"] == 1
    assert warps[3][0, 2] == pytest.approx(5, abs=0.5)
    assert warps[3][1, 2] == pytest.approx(2, abs=0.5)
    detections["3"][0]["shot_id"] = 2
    with patch("pipeline.hwaccel.nvdec_video_capture", return_value=FakeCapture(4)), patch(
        "pipeline.tracking.frame_features.iter_frames", return_value=iter([(0, source), (3, following)])
    ):
        _, warps, _ = tracking_frame_features(Path("synthetic-test.mp4"), detections, 30, timeline)
    assert warps == {}


def test_track_stage_wires_measured_motion_and_appearance_into_adapter() -> None:
    detections = {"0": [{"bbox": [0, 0, 10, 20], "embedding": [1, 0]}],
                  "1": [{"bbox": [50, 0, 60, 20], "embedding": [1, 0]}]}
    warp = np.array([[1, 0, 50], [0, 1, 0]], dtype=float)
    tracker = BoTSORTTracker()
    diagnostics = {"appearance_observations": 2, "camera_pairs_measured": 1, "validated": False}
    with patch("pipeline.tracking.frame_features.tracking_frame_features", return_value=(
        detections, {1: warp}, diagnostics
    )) as features, patch("pipeline.stage_track.backend.create_tracklet", return_value={"id": "track"}):
        result = run("clip", {}, 30, "job", tracker=tracker, video_path=Path("film.mp4"))
    features.assert_called_once()
    assert result["tracklet_count"] == 1
    assert result["tracker_inputs"] == diagnostics
    assert result["tracklets"][0]["provenance"]["tracker_inputs"] == diagnostics
