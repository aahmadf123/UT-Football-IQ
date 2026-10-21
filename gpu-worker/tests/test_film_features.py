"""Measured pixel fixtures exercise actual source-clock feature extraction."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from pipeline.film_features import (
    FeatureConfig,
    camera_residual,
    extract_shot_evidence,
    image_fingerprint,
    measured_view,
    motion_events,
    propose_visual_replays,
)
from pipeline.video_timeline import VideoTimeline


def test_camera_pan_is_removed_before_collective_motion_signal() -> None:
    random = np.random.default_rng(42)
    before = np.zeros((144, 256, 3), np.uint8)
    before[..., 1] = random.integers(70, 160, before.shape[:2], dtype=np.uint8)
    before[..., 0] = random.integers(10, 40, before.shape[:2], dtype=np.uint8)
    shifted = cv2.warpAffine(before, np.float32([[1, 0, 3], [0, 1, 0]]), (256, 144), borderMode=cv2.BORDER_REFLECT)
    result = camera_residual(before, shifted, .1)
    assert result["camera_fit"]
    assert result["active_fraction"] < .08


def test_motion_episodes_require_setup_and_keep_actual_irregular_pts() -> None:
    stamps = [0., .11, .23, .38, .52, .67, .81, .97, 1.12, 1.31, 1.54, 1.79, 2., 2.23, 2.5, 2.8, 3.1, 3.4]
    samples = [{"timestamp_seconds": stamp, "source_frame": i * 3, "timestamp_verified": True,
                "scene_kind": "field", "camera_fit": True, "motion": 15. if .81 <= stamp <= 1.79 else 0.,
                "active_fraction": .2} for i, stamp in enumerate(stamps)]
    events = motion_events(samples)
    assert [event["event_type"] for event in events] == ["motion_onset", "motion_settled"]
    assert events[0]["timestamp_seconds"] == .81
    assert events[1]["timestamp_seconds"] == 2.
    assert all(event["validated"] is False for event in events)
    # Motion at the first picture cannot establish a previously set formation.
    assert motion_events(samples[6:12]) == []


def test_replay_repeat_matches_changing_actual_image_hashes_and_preserves_banner() -> None:
    random = np.random.default_rng(83)
    hashes = [image_fingerprint(random.integers(0, 255, (40, 60, 3), dtype=np.uint8)) for _ in range(8)]
    def scene(sid: str, start: float) -> dict:
        return {"id": sid, "start_seconds": start, "end_seconds": start + 4,
                "events": [], "moving_samples": 5,
                "fingerprints": [{"hash": digest, "timestamp_seconds": start+i*.5, "source_frame": i} for i, digest in enumerate(hashes)]}
    first, replay = scene("live", 0), scene("repeat", 10)
    replay["replay_evidence"] = {"source": "ocr_replay_banner", "validated": False}
    propose_visual_replays([first, replay])
    assert replay["replay_evidence"]["target_shot_id"] == "live"
    assert replay["replay_evidence"]["corroborating_marker"]["source"] == "ocr_replay_banner"
    assert replay["replay_evidence"]["validated"] is False
    for item in (first, replay):
        item.pop("replay_evidence", None)
        for sample in item["fingerprints"]:
            sample["hash"] = hashes[0]
    propose_visual_replays([first, replay])
    assert "replay_evidence" not in replay


def test_grass_without_field_lines_does_not_imply_an_endzone_angle() -> None:
    grass = np.full((144, 256, 3), [15, 100, 15], np.uint8)
    assert measured_view(grass, 1.) == ("unknown", 0.)


def test_real_decode_feature_events_use_native_fractional_source_pts(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-motion-2997.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30000/1001, (256, 144))
    if not writer.isOpened():
        pytest.skip("Synthetic MP4 fixture encoder unavailable")
    random = np.random.default_rng(4)
    field = np.zeros((144, 256, 3), np.uint8)
    field[..., 1] = random.integers(75, 135, field.shape[:2], dtype=np.uint8)
    for frame in range(120):
        image = field.copy()
        offset = max(0, min(30, frame - 36))
        for athlete in range(12):
            x, y = 10 + (athlete % 6) * 34 + offset, 35 + (athlete // 6) * 45
            cv2.rectangle(image, (x, y), (x+8, y+16), (210, 210, 210), -1)
        writer.write(image)
    writer.release()
    timeline = VideoTimeline.from_path(path)
    shots, provenance = extract_shot_evidence(path, timeline=timeline, config=FeatureConfig(sample_hz=10))
    assert provenance["native_pts"] is True and provenance["sample_count"] > 20
    assert shots[0]["start_seconds"] == 0
    assert shots[-1]["end_seconds"] == timeline.duration
    events = [event for shot in shots for event in shot["events"]]
    assert events, "Actual stable→motion fixture must exercise the event proposal path"
    for event in events:
        assert event["timestamp_seconds"] == timeline.timestamp(event["source_frame"])
        assert event["event_type"] in {"motion_onset", "motion_settled"}
        assert event["validated"] is False


def test_camera_audit_keeps_sub_300ms_shots_between_motion_samples(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-rapid-cuts.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30000/1001, (256, 144))
    if not writer.isOpened():
        pytest.skip("Synthetic MP4 fixture encoder unavailable")
    for index in range(18):
        color = (20, 110, 20) if index < 4 else (170, 15, 15) if index < 7 else (15, 15, 170)
        writer.write(np.full((144, 256, 3), color, np.uint8))
    writer.release()
    timeline = VideoTimeline.from_path(path)
    shots, provenance = extract_shot_evidence(path, timeline=timeline)
    assert [shot["start_seconds"] for shot in shots] == [0., timeline.timestamp(4), timeline.timestamp(7)]
    assert provenance["cut_audit"] == "every_decoded_native_frame"
