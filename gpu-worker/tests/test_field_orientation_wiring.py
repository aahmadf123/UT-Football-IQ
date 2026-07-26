"""The measured orientation has to actually reach the stages that need it.

The bug this guards against is not arithmetic: it is a default. ``+1`` used to
be supplied by three separate places when nothing had measured anything, and a
wrong sign there mirrors every depth-behind-LOS feature rather than degrading
it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from pipeline.homography import field_orientation as fo
from pipeline.orchestrator import _offense_direction

# ── orchestrator: label → ±1 / None ───────────────────────────────────────────


def _label(direction: Any) -> list[dict[str, Any]]:
    return [{"label_type": "field_orientation", "label_value": {"direction": direction}}]


@pytest.mark.parametrize("direction", (1, -1))
def test_measured_direction_is_passed_through(direction: int) -> None:
    assert _offense_direction(_label(direction)) == direction


def test_unresolved_orientation_is_none_not_one() -> None:
    """The whole point: an unmeasured orientation must not read as +1."""
    assert _offense_direction(_label(None)) is None


def test_missing_label_is_none() -> None:
    assert _offense_direction([]) is None
    assert _offense_direction([{"label_type": "offensive_formation"}]) is None


def test_play_direction_no_longer_supplies_the_orientation() -> None:
    """It never meant this. Downstream football code reads that label's
    presence as "this play was a run", so it cannot double as the field frame."""
    labels = [{"label_type": "play_direction", "label_value": {"direction": "left"}}]
    assert _offense_direction(labels) is None


def test_garbage_direction_value_is_none() -> None:
    for bad in ("left", 0, 2, "", {}):
        assert _offense_direction(_label(bad)) is None


# ── stage_labels: emits a measured orientation, and no play_direction ─────────


def _track(track_id: str, xs: list[float], y: float) -> dict[str, Any]:
    return {
        "id": track_id,
        "track_points": [
            {"frame_number": i, "field_x": x, "field_y": y, "bbox": [0, 0, 10, 20]}
            for i, x in enumerate(xs)
        ],
    }


def _aligned_formation_tracks() -> list[dict[str, Any]]:
    """Ten frames of a static formation: offence attacking +x (defence deeper)."""
    tracks: list[dict[str, Any]] = []
    spec: list[tuple[float, float]] = []
    for y in (-4.0, -2.0, 0.0, 2.0, 4.0):
        spec.append((49.8, y))
    spec += [(45.0, 0.0), (43.0, 2.0), (49.0, -20.0), (49.0, 20.0)]
    for y in (-3.0, 0.0, 3.0):
        spec.append((51.5, y))
    spec += [(55.0, 0.0), (62.0, -14.0), (62.0, 14.0)]
    for i, (x, y) in enumerate(spec):
        tracks.append(_track(f"t{i}", [x] * 10, y))
    return tracks


def _run_labels(tracklets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from pipeline.stage_labels import run as run_labels

    created: list[dict[str, Any]] = []

    def fake_create_label(label_type: str, label_value: dict[str, Any], **_: Any):
        created.append({"label_type": label_type, "label_value": label_value})
        return {"id": f"label-{len(created)}"}

    with patch(
        "pipeline.stage_labels.backend.create_label", side_effect=fake_create_label
    ):
        run_labels(
            "clip-1",
            tracklets,
            [{"event_type": "snap", "frame_number": 0}],
            fps=30.0,
        )
    return created


def test_stage_labels_emits_a_measured_orientation() -> None:
    created = _run_labels(_aligned_formation_tracks())
    orientation = [x for x in created if x["label_type"] == "field_orientation"]
    assert len(orientation) == 1
    value = orientation[0]["label_value"]
    assert value["direction"] == 1
    assert value["evidence"] == "secondary_depth_vote"
    assert value["frames_resolved"] == 10


def test_stage_labels_no_longer_emits_play_direction() -> None:
    """It was emitted on every play, which made every play a run downstream."""
    created = _run_labels(_aligned_formation_tracks())
    assert not [x for x in created if x["label_type"] == "play_direction"]


def test_uncalibrated_tracks_leave_the_orientation_unresolved() -> None:
    """``field_x or 0`` used to turn "no calibration" into a real coordinate."""
    tracks = [
        {
            "id": f"t{i}",
            "track_points": [
                {"frame_number": f, "field_x": None, "field_y": None, "bbox": [0, 0, 1, 1]}
                for f in range(10)
            ],
        }
        for i in range(16)
    ]
    created = _run_labels(tracks)
    orientation = [x for x in created if x["label_type"] == "field_orientation"][0]
    assert orientation["label_value"]["direction"] is None


# ── __main__: artifact reader ────────────────────────────────────────────────


def _worker_main():
    """Load the worker entrypoint by path — it is ``__main__.py``, not a package."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "__main__.py"
    spec = importlib.util.spec_from_file_location("worker_main_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_artifact_reader_never_defaults_to_one() -> None:
    read = _worker_main()._offense_direction_artifact
    assert read({}) is None
    assert read({"offense_direction": None}) is None
    assert read({"offense_direction": 1}) == 1
    assert read({"offense_direction": -1}) == -1
    assert read({"offense_direction": "-1"}) == -1
    assert read({"offense_direction": "left"}) is None


# ── the guard actually runs inside the calibrate stage ───────────────────────


def test_calibrate_corrects_a_mirrored_fit() -> None:
    """A mirrored fit must not survive ``_best_frame_fit``."""
    from pipeline import stage_calibrate

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    mirrored = np.array(
        [[0.06, 0.0, -20.0], [0.0, 0.05, -18.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    assert fo.is_mirrored(mirrored, frame.shape)

    class _KP:
        src_pts = np.zeros((6, 2))
        dst_pts = np.zeros((6, 2))
        line_count = 10
        field_coverage = 0.5
        yardline_angles: list[float] = []
        reason_codes: list[str] = []

        def has_enough(self) -> bool:
            return True

    with (
        patch.object(stage_calibrate.yk, "grass_mask", return_value=(None, 0.5)),
        patch.object(stage_calibrate.yk, "detect_keypoints", return_value=_KP()),
        patch.object(stage_calibrate.fb, "detect_field_boundary", return_value=None),
        patch.object(
            stage_calibrate.dlt_ransac,
            "ransac_homography",
            return_value=(mirrored, np.ones(6, dtype=bool)),
        ),
    ):
        best = stage_calibrate._best_frame_fit([frame])

    assert best is not None
    H, _kp, _ratio, codes = best
    assert fo.is_mirrored(H, frame.shape) is False
    assert "handedness_corrected" in codes


def test_drone_path_persists_fit_level_reason_codes() -> None:
    """The drone path used to drop them, which is the path this footage takes.

    ``KeypointResult.reason_codes`` only describes line detection. The fit-level
    codes -- ``handedness_corrected``, ``low_inlier_ratio`` -- exist only on what
    ``_best_frame_fit`` returns, and ``_calibrate_drone`` was unpacking them into
    ``_`` and then persisting the keypoint codes instead. A corrected drone
    calibration therefore reached the backend with no sign it had been corrected.
    """
    from pipeline import stage_calibrate

    frames = [np.zeros((720, 1280, 3), dtype=np.uint8) for _ in range(3)]
    H = np.array([[0.06, 0.0, -20.0], [0.0, -0.05, 18.0], [0.0, 0.0, 1.0]])

    class _KP:
        line_count = 12
        field_coverage = 0.6
        yardline_angles = [0.1, 0.11]
        reason_codes = ["rows_observed"]

    fit = (H, _KP(), 0.9, ["rows_observed", "handedness_corrected"])

    with (
        patch.object(stage_calibrate, "_best_frame_fit", return_value=fit),
        patch.object(stage_calibrate, "_field_boundary_diagnostics", return_value=None),
        patch.object(stage_calibrate.backend, "create_calibration", return_value={}),
    ):
        result = stage_calibrate._calibrate_drone(
            "video-1", "job-1", frames, "drone_follow", stage_calibrate.VARIANT_LITE
        )

    assert "handedness_corrected" in result["reason_codes"]
