"""Field coordinates reach the track points, and the stage order that needs them.

Together these cover the seam that made the pipeline produce zeros on real film:
the calibration was computed, POSTed, and then dropped, so nothing ever wrote
``field_x``. Because every reader guards with ``.get("field_x")`` and falls back
to ``or 0``, the failure was completely silent — metrics came out, they were
just meaningless.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from pipeline import stage_track
from pipeline.lightweight_config import NIGHTLY_STAGES, SAME_SESSION_STAGES
from pipeline.orchestrator import CLIP_STAGES, VIDEO_STAGES

# Scale pixels by 1/10 and shift, so expected yards are easy to read off.
SCALED_H = [0.1, 0.0, 0.0, 0.0, 0.1, -20.0, 0.0, 0.0, 1.0]
IDENTITY_H = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


class _FakeTrack:
    def __init__(self, points: list[dict[str, Any]]) -> None:
        self.track_id = "t1"
        self.points = points
        self.start_frame = points[0]["frame_number"] if points else 0
        self.last_frame = points[-1]["frame_number"] if points else 0


class _FakeTracker:
    mask_aware = False

    def __init__(self, tracks: list[_FakeTrack]) -> None:
        self._tracks = tracks

    def track(self, detections: dict[str, Any]) -> list[_FakeTrack]:
        return self._tracks


def _points(count: int = 3) -> list[dict[str, Any]]:
    return [
        {"frame_number": i, "bbox": [100.0 + i * 10, 50.0, 140.0 + i * 10, 130.0]}
        for i in range(count)
    ]


def _run(homography: list[float] | None, analytics_safe: bool) -> dict[str, Any]:
    track = _FakeTrack(_points())
    with patch.object(stage_track.backend, "create_tracklet", return_value={"id": "tr-1"}):
        result = stage_track.run(
            "clip-1",
            {},
            30.0,
            "job-1",
            tracker=_FakeTracker([track]),
            homography=homography,
            analytics_safe=analytics_safe,
        )
    return {"result": result, "points": track.points}


class TestTrackPointProjection:
    def test_writes_field_coordinates(self) -> None:
        out = _run(SCALED_H, analytics_safe=True)
        for point in out["points"]:
            assert point["field_x"] is not None
            assert point["field_y"] is not None

    def test_projects_the_ground_anchor_not_the_box_centre(self) -> None:
        # bbox [100, 50, 140, 130] -> feet at (120, 130) -> (12.0, -7.0).
        # The box centre (120, 90) would give y = -11.0, i.e. four yards off.
        out = _run(SCALED_H, analytics_safe=True)
        first = out["points"][0]
        assert first["field_x"] == pytest.approx(12.0)
        assert first["field_y"] == pytest.approx(-7.0)

    def test_reports_how_many_points_it_projected(self) -> None:
        assert _run(SCALED_H, analytics_safe=True)["result"]["projected_points"] == 3

    def test_writes_nothing_when_calibration_is_not_analytics_safe(self) -> None:
        # A homography nobody trusts produces plausible-looking yard values that
        # are wrong, which is worse than none: the readers treat a missing key
        # as "no spatial metrics" and suppress, but a present-and-wrong value
        # flows into separation, speed and workload as though it were real.
        out = _run(SCALED_H, analytics_safe=False)
        assert out["result"]["projected_points"] == 0
        for point in out["points"]:
            assert "field_x" not in point

    def test_writes_nothing_without_a_homography(self) -> None:
        out = _run(None, analytics_safe=True)
        assert out["result"]["projected_points"] == 0
        for point in out["points"]:
            assert "field_x" not in point

    def test_survives_an_unusable_homography(self) -> None:
        # Calibration can emit a degenerate fit; the clip must still track.
        out = _run([1.0, 2.0, 3.0], analytics_safe=True)
        assert out["result"]["projected_points"] == 0
        assert out["result"]["tracklet_count"] == 1

    def test_the_backend_receives_the_coordinates(self) -> None:
        # The same point dicts go to create_tracklet and to the in-memory
        # tracklets, so mutating in place is what makes both carry them.
        track = _FakeTrack(_points())
        with patch.object(
            stage_track.backend, "create_tracklet", return_value={"id": "tr-1"}
        ) as create:
            stage_track.run(
                "clip-1",
                {},
                30.0,
                "job-1",
                tracker=_FakeTracker([track]),
                homography=SCALED_H,
                analytics_safe=True,
            )
        sent = create.call_args.kwargs["track_points"]
        assert all("field_x" in p for p in sent)

    def test_points_without_a_bbox_are_skipped(self) -> None:
        track = _FakeTrack([{"frame_number": 0}, *_points(2)])
        with patch.object(stage_track.backend, "create_tracklet", return_value={"id": "tr-1"}):
            result = stage_track.run(
                "clip-1",
                {},
                30.0,
                "job-1",
                tracker=_FakeTracker([track]),
                homography=IDENTITY_H,
                analytics_safe=True,
            )
        assert result["projected_points"] == 2
        assert "field_x" not in track.points[0]


class TestStageOrder:
    """The run order lives in lightweight_config, not in CLIP_STAGES.

    ``run_pipeline`` filters its stage list with ``[s for s in stage_list if s
    in CLIP_STAGES]`` — membership only. Reordering CLIP_STAGES alone changes
    nothing at runtime, which is exactly the kind of edit that looks correct in
    review and does nothing.
    """

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_events_precede_pose(self, name: str, stages: list[str]) -> None:
        # stage_pose takes the events list and anchors its biomechanics at the
        # snap. With pose first it was always handed [], so every pose metric
        # was computed against no snap at all.
        assert stages.index("events") < stages.index("pose"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_events_precede_labels(self, name: str, stages: list[str]) -> None:
        # stage_labels reads formation at the snap frame.
        assert stages.index("events") < stages.index("labels"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_track_precedes_events(self, name: str, stages: list[str]) -> None:
        assert stages.index("track") < stages.index("events"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_calibrate_precedes_track(self, name: str, stages: list[str]) -> None:
        # Track projects field coordinates, so it needs the homography.
        assert stages.index("calibrate") < stages.index("track"), name

    @pytest.mark.parametrize(
        ("name", "stages"),
        [("same_session", SAME_SESSION_STAGES), ("nightly", NIGHTLY_STAGES)],
    )
    def test_canonical_order_agrees_with_the_run_lists(self, name: str, stages: list[str]) -> None:
        """CLIP_STAGES documents the order; the run lists decide it.

        They are edited in different files, so without this they drift and the
        documented order becomes a comfortable lie.
        """
        clip_only = [s for s in stages if s in CLIP_STAGES]
        expected = [s for s in CLIP_STAGES if s in stages]
        assert clip_only == expected, name

    def test_video_stages_are_disjoint_from_clip_stages(self) -> None:
        assert not set(VIDEO_STAGES) & set(CLIP_STAGES)


class TestProjectionPlausibility:
    """A homography can be right on one axis and badly scaled on the other.

    That is the live failure on the Toledo indoor footage: the yard lines pin
    the downfield axis exactly, but the two cross-field rows are dashed hash
    marks with no sideline in frame to identify them against, so whether they
    are 13.3 or 26.7 yards apart is an assumption. Guess high and every player
    spreads across ~98 yards of a 53-yard field -- with a perfect inlier ratio,
    because the fit is consistent with its own correspondences.
    """

    def _spread(self, scale: float) -> dict[str, Any]:
        # `scale` is the yards-per-pixel applied to the lateral (field_y) axis;
        # the boxes step down the frame so that axis is the one that spreads.
        H = [0.1, 0.0, 0.0, 0.0, scale, 0.0, 0.0, 0.0, 1.0]
        points = [
            {"frame_number": i, "bbox": [0.0, i * 100.0, 40.0, i * 100.0 + 100]}
            for i in range(6)
        ]
        track = _FakeTrack(points)
        with patch.object(stage_track.backend, "create_tracklet", return_value={"id": "tr-1"}):
            result = stage_track.run(
                "clip-1", {}, 30.0, "job-1",
                tracker=_FakeTracker([track]), homography=H, analytics_safe=True,
            )
        return {"result": result, "points": track.points}

    def test_accepts_a_spread_that_fits_on_a_field(self) -> None:
        out = self._spread(0.05)  # ~5 yards of lateral spread
        assert out["result"]["projected_points"] == 6

    def test_rejects_a_spread_no_field_could_contain(self) -> None:
        out = self._spread(1.0)  # ~100 yards of lateral spread
        assert out["result"]["projected_points"] == 0

    def test_rejection_writes_no_coordinates_at_all(self) -> None:
        # Not "writes some": a partial write is worse, because the readers
        # guard on presence and would treat the survivors as trustworthy.
        out = self._spread(1.0)
        for point in out["points"]:
            assert "field_x" not in point
            assert "field_y" not in point

    def test_rejection_still_produces_tracklets(self) -> None:
        # An uncalibrated clip must still track, render and be reviewable.
        assert self._spread(1.0)["result"]["tracklet_count"] == 1

    def test_out_of_bounds_players_are_not_rejected(self) -> None:
        # Players are carried out of bounds and chase into the bench area; the
        # guard rejects impossible fields, not legal positions.
        assert stage_track._extent_is_possible([(10.0, -30.0), (60.0, 30.0)])

    def test_a_double_width_field_is_rejected(self) -> None:
        assert not stage_track._extent_is_possible([(10.0, -55.0), (60.0, 55.0)])

    def test_a_few_bad_tracks_do_not_veto_a_good_calibration(self) -> None:
        # Detectors fire on field markings and sideline furniture, and those
        # land at the frame edges where projection error is largest. Measuring
        # min-to-max lets one such track speak for the whole clip.
        good = [(50.0, y) for y in range(-20, 21)]  # 41 points, 40 yards wide
        with_outliers = [*good, (50.0, -400.0), (50.0, 400.0)]
        assert stage_track._extent_is_possible(with_outliers)

    def test_a_wrong_scale_is_still_caught_through_the_trim(self) -> None:
        # The trim must not be so generous that a genuinely stretched axis
        # survives it -- that is the failure the guard exists for.
        stretched = [(50.0, float(y)) for y in range(-100, 101, 2)]
        assert not stage_track._extent_is_possible(stretched)

    def test_a_small_sample_keeps_its_full_range(self) -> None:
        # With only a handful of points there is nothing to spare; trimming 5%
        # of six points would discard real signal.
        assert not stage_track._extent_is_possible([(50.0, -60.0), (50.0, 60.0)])
