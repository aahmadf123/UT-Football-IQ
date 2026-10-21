"""Regressions for timing, identity scope and conservative observed geometry."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.media_contract import (
    MEDIA_GENERATOR_VERSION,
    TIMELINE_REVISION,
    current_playback,
    encoder_config,
    validate_duration,
)
from pipeline.observations import bounded_frame_observations, field_position_at
from pipeline.qb_observations import observed_qb_durations
from pipeline.runtime import job_context, set_checkpoint_identity, stable_record_id
from pipeline.segment_models import GameFilmSegmenter
from pipeline.stage_oline import _estimate_los, _gap_measurements, _identify_ol
from pipeline.stage_reid import _identity_conflicts, roster_candidates
from pipeline.tracker_models import get_tracker


def test_old_media_and_encoder_configuration_are_not_reused() -> None:
    media = {"kind": "clean_clip", "source_sha256": "source", "source_start_seconds": 1,
             "source_end_seconds": 2, "generator_version": MEDIA_GENERATOR_VERSION,
             "timeline_revision": TIMELINE_REVISION, "encoder_config": encoder_config(),
             "duration_validation": {"passed": True}, "alignment_validation": {"passed": True}}
    assert current_playback(media, "source", 1, 2)
    assert not current_playback({**media, "generator_version": "obsolete"}, "source", 1, 2)
    assert not current_playback({**media, "encoder_config": encoder_config(15)}, "source", 1, 2)
    assert not current_playback(media, "changed-source", 1, 2)
    assert not current_playback({**media, "duration_validation": None}, "source", 1, 2)


@pytest.mark.parametrize("actual", [19.0, 20.05, 21.0, "nan"])
def test_duration_validation_rejects_more_than_one_output_frame(actual: float | str) -> None:
    import json
    from types import SimpleNamespace

    with patch("pipeline.media_contract.subprocess.run", return_value=SimpleNamespace(
        stdout=json.dumps({"streams": [{"duration": actual, "codec_name": "h264"}]})
    )):
        with pytest.raises(ValueError, match="timing validation"):
            validate_duration(Path("proxy.mp4"), 20, 30)


@pytest.mark.parametrize("variant", ["iou-tracker", "sam3-mask-tracker", "botsort", "strongsort"])
@pytest.mark.parametrize("gap", ["sparse", "pts", "shot"])
def test_every_tracker_expires_before_association(variant: str, gap: str) -> None:
    tracker = get_tracker(variant, max_lost_frames=30)
    tracker.max_lost_seconds = 1.0
    detection = {"bbox": [0, 0, 30, 60], "class": "player", "timestamp_seconds": 0, "shot_id": 1}
    frame = "300" if gap == "sparse" else "1"
    following = {**detection, "timestamp_seconds": 10 if gap != "shot" else 0.03,
                 "shot_id": 2 if gap == "shot" else 1}
    assert len(tracker.track({"0": [detection], frame: [following]})) == 2


def test_duplicate_jersey_candidates_require_unit_and_game_team() -> None:
    roster = [{"id": "qb", "jersey_number": 7, "position": "QB"},
              {"id": "safety", "jersey_number": 7, "position": "S"}]
    assert len(roster_candidates({}, roster)) == 2
    assert [p["id"] for p in roster_candidates({"team_label": "offense"}, roster)] == ["qb"]
    game = [{**p, "_identity_scope": "resolved_team_required", "_roster_team": "Toledo"}
            for p in roster]
    assert roster_candidates({"team_label": "offense"}, game) == []
    membership = {"resolved": True, "team": "Opponent", "evidence": "reviewed_uniform"}
    track = {"team_label": "offense", "provenance": {"team_membership": membership}}
    assert roster_candidates(track, game) == []
    membership["team"] = "Toledo"
    assert [p["id"] for p in roster_candidates(track, game)] == ["qb"]


def test_identity_cannot_duplicate_an_overlapping_athlete_or_ignore_candidates() -> None:
    first = {"id": "a", "track_points": [{"frame_number": 0}, {"frame_number": 10}]}
    second = {"id": "b", "track_points": [{"frame_number": 5}, {"frame_number": 20}]}
    assert _identity_conflicts(second, "player", [first, second], {"a": "player"})
    second["provenance"] = {"identity": {"candidates": ["other-player"]}}
    assert _identity_conflicts(second, "player", [second], {})


def test_geometry_never_crosses_a_lost_observation_or_long_gap() -> None:
    points = [{"frame_number": 0, "timestamp_seconds": 0, "field_x": 0, "field_y": 0},
              {"frame_number": 3, "timestamp_seconds": 0.1, "visibility_state": "lost"},
              {"frame_number": 6, "timestamp_seconds": 0.2, "field_x": 6, "field_y": 0}]
    assert field_position_at({"track_points": points}, 3) is None
    assert field_position_at({"track_points": points}, 2) is None
    assert field_position_at({"track_points": [points[0], {**points[-1], "frame_number": 300,
                                                            "timestamp_seconds": 10}]}, 150) is None


def test_vfr_geometry_requires_the_target_presentation_timestamp() -> None:
    track = {"track_points": [
        {"frame_number": 0, "timestamp_seconds": 0, "field_x": 0, "field_y": 0},
        {"frame_number": 3, "timestamp_seconds": 0.2, "field_x": 6, "field_y": 0},
    ]}
    assert field_position_at(track, 1) is None
    assert field_position_at(track, 1, timestamp_seconds=0.15) == pytest.approx((4.5, 0))


def test_oline_spacing_preserves_initial_neighbors_and_uses_full_field_width() -> None:
    tracks = [{"id": str(i), "team_label": "offense", "track_points": [
        {"frame_number": 0, "field_x": 50, "field_y": 5 + i * 2},
        {"frame_number": 3, "field_x": 50, "field_y": 15 - i * 2},
    ]} for i in range(5)]
    selected = _identify_ol(tracks, 0, 50)
    assert len(selected) == 5  # Offensive alignment need not be centered at y=0.
    measurements = _gap_measurements(selected, 0, 3, 30)
    assert measurements[0]["initial_pair"] == ["0", "1"]
    assert measurements[0]["geometry"] == "center_to_center"
    assert measurements[0]["clear_lane_width"] is None
    assert _estimate_los([], 0) is None


def test_multishot_extract_without_scorecards_preserves_unresolved_shots() -> None:
    from pipeline.game_film import GameFilmStructure, Shot

    structure = GameFilmStructure(edited=False, shots=[Shot(0, 8, 0.7), Shot(8, 12, 0.7)])
    with patch("pipeline.game_film.analyze", return_value=structure):
        segmenter = GameFilmSegmenter()
        assert segmenter.segment(Path("extract.mp4")) == []
    assert len(segmenter.clip_specs) == 2
    assert all(s["canonical_count_eligible"] is False for s in segmenter.clip_specs)
    assert segmenter.structure["shot_boundaries_seconds"] == [0, 8]


def test_reprocessing_output_ids_do_not_mutate_prior_published_run() -> None:
    with job_context():
        set_checkpoint_identity("same-artifact", "same-source")
        first = stable_record_id("/api/v1/metrics", {"job_id": "first", "metric_name": "speed"})
        retry = stable_record_id("/api/v1/metrics", {"job_id": "first", "metric_name": "speed"})
        replacement = stable_record_id("/api/v1/metrics", {"job_id": "second", "metric_name": "speed"})
    assert first == retry
    assert first != replacement


def test_qb_holding_preserves_endpoint_and_early_pressure() -> None:
    quarterback = {"id": "qb", "observed_role": "QB", "role_resolved": True,
                   "track_points": [{"frame_number": 0, "timestamp_seconds": 10},
                                    {"frame_number": 120, "timestamp_seconds": 14}]}
    events = [{"event_type": "snap", "timestamp_seconds": 10},
              {"event_type": "pressure", "timestamp_seconds": 10.8, "source": "human"},
              {"event_type": "pressure", "timestamp_seconds": 12.8, "source": "human"},
              {"event_type": "throw", "timestamp_seconds": 13}]
    metrics = observed_qb_durations([quarterback], events, 30)
    assert metrics[0]["metric_value"]["seconds"] == 3
    assert metrics[0]["metric_value"]["endpoint"] == "throw"
    assert metrics[1]["metric_value"]["seconds"] == 0.8
    # Proximity candidates never become pressure just by passing a threshold.
    events[1]["source"] = events[2]["source"] = "proximity_heuristic"
    assert len(observed_qb_durations([quarterback], events, 30)) == 1
    censored = observed_qb_durations([quarterback], [events[0]], 30)[0]["metric_value"]
    assert censored["right_censored"] is True and censored["seconds"] == 4


def test_human_event_timing_can_qualify_without_approving_tracking() -> None:
    from pipeline.stage_metrics import run

    events = [{"event_type": "snap", "frame_number": 0, "timestamp_seconds": 0, "source": "human"},
              {"event_type": "throw", "frame_number": 75, "timestamp_seconds": 2.5, "source": "human"}]
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        result = run("clip", [], events, False, 30, "job")
    value = result["metrics"][0]["metric_value"]
    assert value["seconds"] == 2.5
    assert value["provenance"]["validation_source"] == "human_event_timestamps"
    assert value["definition_version"] == "2026-10-21.1"


def test_exported_overlay_interpolates_only_short_same_shot_observations() -> None:
    from pipeline.video_timeline import VideoTimeline

    timeline = VideoTimeline(tuple(i * 1001 / 60000 for i in range(700)), 12, 60000 / 1001)
    first = {"frame_number": 0, "bbox": [0, 0, 10, 20], "shot_id": 1}
    second = {"frame_number": 2, "bbox": [2, 0, 12, 20], "shot_id": 1}
    output = bounded_frame_observations({"track_points": [first, second]}, timeline, timeline.fps)
    assert output[1]["frame_number"] == 1 and output[1]["interpolated"]
    assert output[1]["bbox"][0] == pytest.approx(1)
    for replacement in ({**second, "frame_number": 600}, {**second, "shot_id": 2}):
        output = bounded_frame_observations({"track_points": [first, replacement]}, timeline, timeline.fps)
        assert not any(p["interpolated"] for p in output)
    lost = {"frame_number": 1, "visibility_state": "lost"}
    output = bounded_frame_observations({"track_points": [first, lost, second]}, timeline, timeline.fps)
    assert not any(p["interpolated"] for p in output)


def test_unapproved_identity_candidate_cannot_become_a_roster_name() -> None:
    from pipeline import stage_reid

    track = {"id": "track", "track_points": [{"frame_number": 0, "bbox": [0, 0, 10, 20]}]}
    with patch.object(stage_reid, "_identify_tracklet", return_value="qb"), patch.object(
        stage_reid, "_get_ocr_adapter", return_value=None
    ), patch.object(stage_reid, "_get_reid_adapter", return_value=None), patch.object(
        stage_reid.cv2, "VideoCapture"
    ), patch.object(stage_reid.VideoTimeline, "from_path"), patch.object(
        stage_reid, "_patch_tracklet"
    ) as association:
        result = stage_reid.run("clip", Path("source.mp4"), ["track"], [track],
                                [{"id": "qb", "jersey_number": 7}], "", priority=10)
    association.assert_not_called()
    assert result["assigned"] == 0 and result["unapproved_candidate_count"] == 1
    enriched = result["tracklets"][0]
    assert not enriched.get("player_id")
    assert enriched["provenance"]["identity"]["candidate_player_id"] == "qb"


def test_reprocessing_consumes_staff_split_merge_windows_without_regenerating_ids() -> None:
    from pipeline.orchestrator import PipelineContext, _run_video_stage

    windows = [{"id": "staff-child", "start_time": 2, "end_time": 6, "label_data": {}}]
    context = PipelineContext("video", "source.mp4")
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=windows), patch(
        "pipeline.stage_segment.run"
    ) as segmenter:
        result = _run_video_stage("segment", context)
    segmenter.assert_not_called()
    assert context.clips == windows
    assert result["data_source"] == "manual"
    assert result["boundary_model"] == "staff-edited-layout"
    with patch("pipeline.backend.fetch_boundary_overrides", side_effect=RuntimeError("connection lost")):
        with pytest.raises(RuntimeError, match="connection lost"):
            _run_video_stage("segment", context)


def test_staff_layout_keeps_shot_resets_and_blocks_cross_cut_spatial_projection() -> None:
    from pipeline.game_film import Shot
    from pipeline.orchestrator import PipelineContext, _run_clip_stage, _run_video_stage

    common = {"capture_regime": "fixed_sideline", "label_data": {"view": "sideline"}}
    windows = [{**common, "id": "merged", "start_time": 0, "end_time": 10},
               {**common, "id": "next", "start_time": 10, "end_time": 15}]
    context = PipelineContext("video", "source.mp4", capture_regime="fixed_sideline")
    context.video_path = Path("source.mp4")
    context.calibration = {"analytics_safe": True, "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}
    context.detections = {str(frame): [{"timestamp_seconds": time}]
                          for frame, time in ((147, 4.9), (150, 5), (306, 10.2))}
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=windows), patch(
        "pipeline.game_film.detect_shots", return_value=[Shot(0, 5, .8), Shot(5, 10, .8), Shot(10, 15, .8)]
    ), patch("pipeline.stage_segment.run") as segmenter:
        artifacts = _run_video_stage("segment", context)
    segmenter.assert_not_called()
    assert artifacts["clips"] == windows  # No inferred play boundaries replace staff edits.
    assert artifacts["spatially_unresolved_clip_ids"] == ["merged"]
    assert artifacts["game_film"]["shot_boundaries_seconds"] == [0, 5, 10]
    context.stage_artifacts["segment"] = artifacts
    with patch("pipeline.stage_track.run", return_value={}) as tracker:
        _run_clip_stage("track", context, windows[0])
        detections = tracker.call_args.args[1]
        assert detections["147"][0]["shot_id"] != detections["150"][0]["shot_id"]
        assert tracker.call_args.kwargs["analytics_safe"] is False
        assert tracker.call_args.kwargs["homography"] is None
        _run_clip_stage("track", context, windows[1])
        assert tracker.call_args.kwargs["analytics_safe"] is True
        assert tracker.call_args.kwargs["homography"] == context.calibration["homography"]


def test_staff_unknown_merge_cannot_inherit_recording_calibration_and_audit_failure_stops() -> None:
    from pipeline.game_film import Shot
    from pipeline.orchestrator import PipelineContext, _run_video_stage

    window = {"id": "merged", "start_time": 0, "end_time": 10,
              "label_data": {"logical_play_status": "staff_merge"}}
    context = PipelineContext("video", "source.mp4", capture_regime="fixed_sideline")
    context.video_path = Path("source.mp4")
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=[window]), patch(
        "pipeline.game_film.detect_shots", return_value=[Shot(0, 10, .8)]
    ):
        result = _run_video_stage("segment", context)
    assert result["spatially_unresolved_clip_ids"] == ["merged"]
    window["capture_regime"] = "unknown"
    window["label_data"]["view"] = "unknown"
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=[window]), patch(
        "pipeline.game_film.detect_shots", return_value=[Shot(0, 10, .8)]
    ):
        result = _run_video_stage("segment", context)
    assert result["spatially_unresolved_clip_ids"] == ["merged"]
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=[window]), patch(
        "pipeline.game_film.detect_shots", return_value=[]
    ), pytest.raises(ValueError, match="decoded no source frames"):
        _run_video_stage("segment", context)


def test_staff_post_cut_split_without_view_cannot_reuse_initial_recording_transform() -> None:
    from pipeline.game_film import Shot
    from pipeline.orchestrator import PipelineContext, _run_video_stage

    windows = [{"id": "before", "start_time": 0, "end_time": 5,
                "capture_regime": "fixed_sideline", "label_data": {"logical_play_status": "staff_split"}},
               {"id": "after", "start_time": 5, "end_time": 10,
                "capture_regime": "fixed_sideline", "label_data": {"logical_play_status": "staff_split"}},
               {"id": "known-view", "start_time": 5, "end_time": 10,
                "capture_regime": "fixed_sideline", "label_data": {"view": "sideline"}}]
    context = PipelineContext("video", "source.mp4", capture_regime="fixed_sideline")
    context.video_path = Path("source.mp4")
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=windows), patch(
        "pipeline.game_film.detect_shots", return_value=[Shot(0, 5, .8), Shot(5, 10, .8)]
    ):
        result = _run_video_stage("segment", context)
    assert result["spatially_unresolved_clip_ids"] == ["after"]
    assert result["clips"] == windows


def test_position_chart_packets_carry_real_series_and_preserve_loss_barriers() -> None:
    from pipeline.stage_oline import run

    tracks = [{"id": str(i), "team_label": "offense", "track_points": [
        {"frame_number": frame, "timestamp_seconds": frame / 30,
         "field_x": 50, "field_y": 5 + 2*i, "source": "synthetic_test",
         "visibility_state": "lost" if i == 0 and frame == 3 else "observed"}
        for frame in (0, 3, 6)
    ]} for i in range(5)]
    with patch("pipeline.stage_oline.backend.create_metric", return_value={"id": "metric"}):
        result = run("clip", tracks, [{"event_type": "snap", "frame_number": 0},
                                     {"event_type": "handoff", "frame_number": 6}], True, 30, "job")
    spacing = next(m for m in result["metrics"] if m["metric_name"] == "geometric_spacing")
    values = spacing["metric_value"]["series"]
    assert [value["timestamp_seconds"] for value in values] == [0, 0.1, 0.2]
    assert values[0]["width_yards"] == 2
    assert values[1]["visibility_state"] == "lost" and "width_yards" not in values[1]
    assert spacing["metric_value"]["provenance"]["source"] == "synthetic_test"


@pytest.mark.parametrize("direction,release_x,expected", [(1, 48, 2), (-1, 52, 2), (1, 52, -2)])
def test_qb_depth_pairs_with_release_track_and_preserves_signed_direction(direction, release_x, expected) -> None:
    from pipeline.stage_metrics import run

    track = {"id": "qb", "observed_role": "QB", "role_resolved": True, "track_points": [
        {"frame_number": frame, "timestamp_seconds": frame / 30,
         "field_x": x, "field_y": 0, "source": "synthetic_test"}
        for frame, x in ((0, 50), (3, (50 + release_x) / 2), (6, release_x))
    ]}
    events = [{"event_type": "snap", "frame_number": 0, "timestamp_seconds": 0},
              {"event_type": "throw", "frame_number": 6, "timestamp_seconds": 0.2}]
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        result = run("clip", [track], events, True, 30, "job", attacking_direction=direction)
    by_name = {m["metric_name"]: m for m in result["metrics"]}
    assert by_name["time_to_throw"]["tracklet_id"] == by_name["dropback_depth"]["tracklet_id"] == "qb"
    assert by_name["dropback_depth"]["metric_value"]["yards"] == expected
    assert len(by_name["distance_traveled"]["metric_value"]["trajectory"]) == 3


def test_shot_resampling_keeps_the_final_source_frame_tail() -> None:
    from pipeline.stage_segment import _write_spec_clips

    with patch("pipeline.stage_segment.backend.create_clip", side_effect=lambda _video, **body: {"id": "clip", **body}) as create:
        _write_spec_clips("video", [{"start_time": 0, "end_time": 30, "play_number": 1,
                                     "canonical_count_eligible": False}], 30.013317, "job", "preliminary")
    assert create.call_args.kwargs["end_time"] == 30.013317
