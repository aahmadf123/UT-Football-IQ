"""Regression gates for real CPU processing and unavailable observations."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from pipeline.detector_models import StubDetector, build_player_detector
from pipeline.homography.timeline import calibration_timeline
from pipeline.pose_estimator import get_estimator
from pipeline.runtime import ModelUnavailableError, adapter_provenance, inference_device
from pipeline.stage_metrics import _distance_traveled, _frame_speeds
from pipeline.stage_metrics import run as run_metrics
from pipeline.stage_pose import run as run_pose
from pipeline.stage_track import _project_tracks


def test_cpu_default_and_explicit_unavailable_gpu(monkeypatch):
    monkeypatch.delenv("PIPELINE_DEVICE", raising=False)
    assert inference_device() == "cpu"
    monkeypatch.setenv("PIPELINE_DEVICE", "cuda")
    fake = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    with patch.dict("sys.modules", {"torch": fake}):
        with pytest.raises(ModelUnavailableError, match="unavailable"):
            inference_device()


def test_cpu_does_not_tile_unknown_camera_without_opt_in(monkeypatch):
    monkeypatch.delenv("DETECT_TILING_ENABLED", raising=False)
    detector = StubDetector()
    assert build_player_detector("stub", "unconstrained", base=detector) is detector
    assert adapter_provenance(detector, "yolov8n")["source"] == "synthetic_test"


def test_one_frame_pixel_boundary_does_not_filter_a_panning_recording(tmp_path):
    from pipeline.stage_detect import run as run_detect

    source = tmp_path / "temporary-video.mp4"
    source.touch()
    with patch("pipeline.stage_detect.object_store.download_to_temp", return_value=source), patch(
        "pipeline.stage_detect._detect", return_value={}
    ) as detect:
        run_detect("v", "video.mp4", "j", detector=StubDetector(),
                   capture_regime="drone_follow", field_boundary={"found": True})
    assert detect.call_args.args[-1] is None


def test_unconfigured_pose_does_not_write_synthetic_keypoints():
    assert get_estimator(None) is None
    with patch("pipeline.stage_pose.backend") as backend:
        result = run_pose("c", SimpleNamespace(), [{"id": "t"}], [], False, 30, "j")
    assert result["skipped"] is True
    backend.create_pose_keypoints.assert_not_called()
    backend.create_metric.assert_not_called()


def test_pose_is_cropped_attributed_and_remapped_for_each_player(monkeypatch):
    monkeypatch.delenv("POSE_EXPERIMENTAL_METRICS", raising=False)
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    image[:, :40] = 5
    image[:, 100:140] = 77

    class Source:
        def iter_frames(self, stride=1):
            yield 0, image
            yield 3, image
            yield 6, image

    class Estimator:
        provenance = {"source": "real", "effective_variant": "test-crop", "device": "cpu"}

        def __init__(self):
            self.seen = []

        def estimate(self, crop):
            self.seen.append((crop.shape, int(crop[0, 0, 0])))
            return [{"name": "nose", "x": 20., "y": 40., "confidence": .9}]

    estimator = Estimator()
    tracks = [{"id": tid, "start_frame": 3, "end_frame": 3,
               "track_points": [{"frame_number": 3, "bbox": bbox}]}
              for tid, bbox in [("left", [0, 0, 40, 80]), ("right", [100, 0, 140, 80])]]
    with patch("pipeline.stage_pose.backend") as backend:
        backend.create_pose_keypoints.return_value = {"id": "kp"}
        result = run_pose("c", Source(), tracks, [], False, 30, "j",
                          estimator=estimator, frame_start=3, frame_end=3)
    calls = [c.kwargs for c in backend.create_pose_keypoints.call_args_list]
    assert [(c["tracklet_id"], c["frame_number"], c["keypoints"][0]["x"]) for c in calls] == [
        ("left", 3, 20.), ("right", 3, 120.)]
    assert estimator.seen == [((80, 40, 3), 5), ((80, 40, 3), 77)]
    assert result["metric_count"] == 0  # raw pose is not a validated mechanics score


class Capture:
    def __init__(self):
        self.index = 0

    def get(self, _property):
        return 30

    def isOpened(self):
        return True

    def read(self):
        if self.index >= 7:
            return False, None
        self.index += 1
        return True, np.zeros((100, 100, 3), np.uint8)

    def release(self):
        pass


def _fit(_frames):
    kp = SimpleNamespace(line_count=20, yardline_angles=[0., 0.], field_coverage=1.)
    return np.diag([.1, .1, 1.]), kp, 1., []


def test_moving_camera_uses_inverse_ecc_per_observed_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("DETECT_FRAME_STEP", "3")
    warp = np.array([[1., 0., 10.], [0., 1., 0.]])
    with patch("cv2.VideoCapture", return_value=Capture()), patch(
        "pipeline.homography.timeline.ecc.estimate_warp", return_value=(warp, True)
    ):
        timeline = calibration_timeline(tmp_path / "video.mp4", _fit)
    points = [{"frame_number": frame, "bbox": [18 + frame / 3 * 10, 10, 22 + frame / 3 * 10, 20]}
              for frame in [0, 3, 6]]
    track = SimpleNamespace(points=points)
    assert _project_tracks([track], None, True, calibration_series=timeline,
                           capture_regime="drone_follow") == 3
    assert [point["field_x"] for point in points] == pytest.approx([2., 2., 2.])


def test_camera_alignment_failure_does_not_reuse_old_field_map(tmp_path, monkeypatch):
    monkeypatch.setenv("DETECT_FRAME_STEP", "3")
    with patch("cv2.VideoCapture", return_value=Capture()), patch(
        "pipeline.homography.timeline.ecc.estimate_warp", return_value=(np.eye(2, 3), False)
    ):
        timeline = calibration_timeline(tmp_path / "video.mp4", _fit)
    assert timeline[0]["analytics_safe"]
    assert not timeline[1]["analytics_safe"]
    assert timeline[1]["homography"] is None
    point = {"frame_number": 3, "bbox": [18, 10, 22, 20]}
    assert _project_tracks([SimpleNamespace(points=[point])], np.eye(3).flatten().tolist(),
                           True, capture_regime="drone_follow") == 0
    assert "field_x" not in point


def test_unsafe_recording_cannot_project_even_with_partial_series():
    point = {"frame_number": 0, "bbox": [18, 10, 22, 20]}
    series = [{"frame_number": 0, "homography": np.eye(3).flatten().tolist(), "analytics_safe": True}]
    assert _project_tracks([SimpleNamespace(points=[point])], None, False,
                           calibration_series=series, capture_regime="drone_follow") == 0
    assert "field_x" not in point


def test_required_result_write_failure_cannot_report_success():
    from pipeline.stage_track import run as run_tracking

    track = SimpleNamespace(points=[{"frame_number": 0}, {"frame_number": 3}],
                            track_id=1, start_frame=0, last_frame=3)
    tracker = SimpleNamespace(track=lambda _: [track], mask_aware=False)
    with patch("pipeline.stage_track.backend.create_tracklet", side_effect=RuntimeError("disk full")):
        with pytest.raises(RuntimeError, match="disk full"):
            run_tracking("c", {}, 30, "j", tracker=tracker)


@pytest.mark.parametrize("point_source,detector_source", [("synthetic_test", "real"), ("real", "synthetic_test")])
def test_synthetic_track_provenance_is_persisted_and_cannot_inherit_detector_validation(point_source, detector_source):
    from pipeline.stage_track import run as run_tracking

    detections = {str(frame): [{"bbox": [10, 10, 30, 50], "confidence": .9, "source": point_source}]
                  for frame in [0, 3]}
    with patch("pipeline.stage_track.backend.create_tracklet", return_value={"id": "track"}) as create:
        result = run_tracking("c", detections, 30, "j", variant="iou-tracker",
                              detector_provenance={"source": detector_source, "validated": True,
                                                   "effective_variant": "yolov8n"})
    provenance = create.call_args.kwargs["provenance"]
    assert provenance["source"] == "synthetic_test"
    assert provenance["validated"] is False
    assert provenance["detector_validated"] is False
    assert result["tracklets"][0]["provenance"] == provenance


def test_synthetic_track_labels_have_explicit_persisted_source():
    from pipeline.stage_labels import run as run_labels

    tracks = [{"id": "track", "provenance": {"source": "synthetic_test"},
               "track_points": [{"frame_number": 0, "bbox": [10, 10, 30, 50]}]}]
    with patch("pipeline.stage_labels.backend.create_label", return_value={"id": "label"}) as create:
        run_labels("clip", tracks, [{"event_type": "snap", "frame_number": 0}], 30)
    assert create.call_count > 0
    assert all(call.kwargs["source"] == "synthetic_test" for call in create.call_args_list)


def test_synthetic_team_classification_is_not_persisted_as_model_source():
    from pipeline.stage_labels import _write_team_labels

    result = SimpleNamespace(label="offense", confidence=.8, frame_number=0,
                             reason="test", stripe_score=0.)
    with patch("pipeline.stage_labels.backend.patch_tracklet_team_label"), patch(
        "pipeline.stage_labels.backend.create_label", return_value={"id": "label"}
    ) as create:
        _write_team_labels("clip", {"track": result}, synthetic=True)
    assert create.call_args.kwargs["source"] == "synthetic_test"
    assert create.call_args.args[1]["provenance"] == {"source": "synthetic_test", "validated": False}


def test_missing_coordinates_cannot_become_origin_speed_or_distance():
    points = [{"frame_number": 0, "field_x": 10., "field_y": 20.},
              {"frame_number": 3},
              {"frame_number": 6, "field_x": 12., "field_y": 20.}]
    assert _frame_speeds(points, 30) == []
    assert _distance_traveled(points) == 0
    with patch("pipeline.stage_metrics.backend") as backend:
        result = run_metrics("c", [{"id": "t", "track_points": points}], [], True, 30, "j")
    assert result["metrics"] == []
    backend.create_metric.assert_not_called()


def test_throw_without_snap_is_not_time_to_throw():
    with patch("pipeline.stage_metrics.backend") as backend:
        result = run_metrics("c", [], [{"event_type": "throw", "frame_number": 30}], False, 30, "j")
    assert result["metrics"] == []
    backend.create_metric.assert_not_called()


def test_cross_recording_box_transfer_is_quarantined():
    from training.cross_regime_distill import run_nightly

    with pytest.raises(RuntimeError, match="SAME target"):
        run_nightly([], video_dir=None)
