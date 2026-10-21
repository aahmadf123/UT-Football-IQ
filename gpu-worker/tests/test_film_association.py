"""Synthetic evidence fixtures for conservative source-time film association."""

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.film_association import associate_shots


def event(kind: str, stamp: float, **extra: object) -> dict:
    return {"event_type": kind, "timestamp_seconds": stamp, "source": "human", "confidence": 1., **extra}


def shot(sid: str, start: float, end: float, *, snap: float | None = None,
         terminal: float | None = None, view: str = "sideline", board: dict | None = None) -> dict:
    events = ([] if snap is None else [event("snap", snap)]) + ([] if terminal is None else [event("dead_ball", terminal)])
    return {"id": sid, "start_seconds": start, "end_seconds": end, "view": view,
            "view_confidence": .8, "scene_kind": "field", "events": events, "scoreboard": board or {}}


def actions(packet: dict) -> list[dict]:
    return [s for s in packet["sequences"] if s["sequence_kind"] not in {"context", "unresolved"}]


def test_short_extract_without_cards_uses_observed_events_and_three_two_second_padding() -> None:
    result = associate_shots([shot("short", 0, 13, snap=5, terminal=9)], duration=13)
    action = actions(result)[0]
    assert (action["start_seconds"], action["snap_seconds"], action["end_seconds"]) == (2, 5, 11)
    assert action["canonical_count_eligible"] is True
    assert result["coverage"]["covered_seconds"] == 13
    assert result["coverage"]["gaps"] == []


def test_replay_is_linked_and_never_adds_a_canonical_snap() -> None:
    live = shot("live", 0, 12, snap=3, terminal=9)
    replay = shot("replay", 12, 25, snap=15, terminal=22)
    replay["replay_evidence"] = {"source": "human", "target_shot_id": "live", "confidence": 1.}
    result = associate_shots([live, replay], duration=25, film_type="television")
    rows = actions(result)
    assert rows[1]["sequence_kind"] == "replay"
    assert rows[0]["canonical_id"] == rows[1]["canonical_id"]
    assert rows[1]["canonical_count_eligible"] is False
    assert sum(row["canonical_count_eligible"] for row in rows) == 1


def test_missing_cards_multiple_angles_require_corroborating_situation_and_event_pattern() -> None:
    board = {"quarter": 1, "clock_seconds": 745, "down": 2, "distance": 7}
    first = shot("side", 0, 12, snap=3, terminal=9, board=board)
    second = shot("end", 12, 24, snap=15, terminal=21, view="endzone", board=board)
    result = associate_shots([first, second], duration=24, film_type="coach_game")
    rows = actions(result)
    assert len(result["associations"]) == 1
    assert result["associations"][0]["kind"] == "alternate_angle"
    assert result["associations"][0]["status"] == "proposed"
    assert rows[0]["canonical_id"] != rows[1]["canonical_id"]
    assert rows[1]["association_candidate"]["canonical_id"] == rows[0]["canonical_id"]
    assert rows[1]["canonical_count_eligible"] is True  # A proposal cannot overrule human observations.
    second["scoreboard"] = {**board, "clock_seconds": 732, "down": 3}
    different = associate_shots([first, second], duration=24, film_type="coach_game")
    assert different["associations"] == []
    assert len({row["canonical_id"] for row in actions(different)}) == 2


def test_unknown_cuts_and_nonfield_context_do_not_invent_events_or_drop_source() -> None:
    unknown = shot("unknown", 0, 2, view="unknown")
    crowd = {**shot("crowd", 2, 4), "scene_kind": "crowd"}
    graphic = {**shot("graphic", 4, 5), "scene_kind": "graphic"}
    commercial = {**shot("commercial", 5, 8), "scene_kind": "commercial"}
    result = associate_shots([unknown, crowd, graphic, commercial], duration=8, film_type="television")
    assert all(not row["canonical_count_eligible"] for row in result["sequences"])
    assert all(row["snap_seconds"] is None for row in result["sequences"])
    assert result["coverage"]["covered_seconds"] == 8
    assert result["coverage"]["gaps"] == []


def test_no_play_labels_remain_explicit_without_an_invented_snap() -> None:
    source = shot("aborted", 0, 6)
    source["events"] = [event("false_start", 3, counts_as_play=False)]
    result = associate_shots([source], duration=6)
    row = actions(result)[0]
    assert row["sequence_kind"] == "false_start"
    assert row["snap_seconds"] is None
    assert row["canonical_count_eligible"] is False


def test_source_pts_are_not_rounded_or_rebuilt_using_nominal_frame_rate() -> None:
    snap, terminal = 5.123456789, 9.078901234
    result = associate_shots([shot("vfr", .016683, 13.013317, snap=snap, terminal=terminal)], duration=13.013317)
    row = actions(result)[0]
    assert row["snap_seconds"] == snap
    assert row["start_seconds"] == snap - 3
    assert row["end_seconds"] == terminal + 2
    assert result["coverage"]["covered_seconds"] == pytest.approx(13.013317)


def test_unvalidated_motion_onsets_are_proposals_and_truncated_live_action_abstains() -> None:
    source = shot("motion", 0, 10)
    source["events"] = [event("motion_onset", 3, source="camera_compensated_flow", validated=False),
                        event("motion_settled", 8, source="camera_compensated_flow", validated=False)]
    row = actions(associate_shots([source], duration=10))[0]
    assert row["snap_seconds"] is None and row["snap_candidate_seconds"] == 3
    assert row["canonical_count_eligible"] is False
    truncated = actions(associate_shots([shot("cut", 0, 8, snap=3)], duration=8))[0]
    assert truncated["live_action_complete"] is False
    assert truncated["canonical_count_eligible"] is False


def test_multiple_event_sequences_in_continuous_shot_do_not_need_camera_cuts() -> None:
    source = shot("continuous", 0, 30)
    source["events"] = [event("snap", 5), event("dead_ball", 10), event("snap", 20), event("dead_ball", 25)]
    result = associate_shots([source], duration=30, film_type="practice")
    rows = actions(result)
    assert [row["snap_seconds"] for row in rows] == [5, 20]
    assert len({row["canonical_id"] for row in rows}) == 2
    assert result["coverage"]["covered_seconds"] == 30


def test_replay_banner_without_unique_target_excludes_count_but_does_not_force_a_link() -> None:
    source = shot("broadcast-replay", 0, 12, snap=3, terminal=9)
    for observation in source["events"]:
        observation["source"] = "unvalidated_detector"
    source["replay_evidence"] = {"source": "ocr_replay_banner", "confidence": .8, "validated": False}
    result = associate_shots([source], duration=12, film_type="television")
    row = actions(result)[0]
    assert row["sequence_kind"] == "replay_candidate"
    assert row["canonical_count_eligible"] is False
    assert row["association_status"] == "unresolved"
    assert result["associations"] == []


def test_out_of_shot_events_are_rejected_without_losing_reviewable_intervals() -> None:
    source = shot("broken-evidence", 0, 8, snap=30, terminal=float("nan"))
    before = copy.deepcopy(source)
    result = associate_shots([source], duration=8)
    assert result["diagnostics"]["rejected_events"] == 2
    assert result["coverage"]["covered_seconds"] == 8
    assert result["sequences"][0]["canonical_count_eligible"] is False
    assert source["events"][0] == before["events"][0]


def test_flat_automatic_validation_flags_cannot_qualify_canonical_counts() -> None:
    source = shot("unqualified", 0, 12, snap=3, terminal=9)
    for observation in source["events"]:
        observation.update(source="real", validated=True, model_version_id="not-evaluation-evidence", film_category="practice")
    row = actions(associate_shots([source], duration=12, film_type="practice"))[0]
    assert row["canonical_count_eligible"] is False


def test_false_start_does_not_suppress_a_later_valid_rep_in_same_continuous_shot() -> None:
    source = shot("practice", 0, 30, snap=20, terminal=25)
    source["events"].insert(0, event("false_start", 3, counts_as_play=False))
    result = associate_shots([source], duration=30, film_type="practice")
    rows = actions(result)
    assert sum(row["canonical_count_eligible"] for row in rows) == 1
    false_start = next(row for row in rows if row["sequence_kind"] == "false_start")
    assert false_start["end_seconds"] < 20


def test_production_analysis_uses_event_association_without_minimum_card_history() -> None:
    from pipeline import game_film
    from pipeline.video_timeline import VideoTimeline

    evidence = [shot("extract", 0, 10, snap=3, terminal=8)]
    timeline = VideoTimeline((0., 3., 8.), 10., .3)
    with patch("pipeline.film_features.extract_shot_evidence", return_value=(evidence, {"source": "test_fixture"})), patch(
        "pipeline.game_film.read_shot_text_evidence", return_value=None
    ):
        structure = game_film.analyze(Path("extract.mp4"), timeline=timeline)
    assert structure.clip_specs
    assert structure.summary()["format_strategy"] == "event_sequence_association"
    assert structure.summary()["sequences"][0]["snap_seconds"] == 3


def test_persisted_clip_spec_keeps_event_pts_and_association_provenance() -> None:
    from pipeline.stage_segment import _write_spec_clips

    result = associate_shots([shot("source", 0, 15, snap=5.123456789, terminal=10.23456789)], duration=15)
    with patch("pipeline.stage_segment.backend.create_clip", side_effect=lambda _video, **body: {"id": "clip", **body}) as create:
        _write_spec_clips("video", result["clip_specs"], 15, "job", "preliminary")
    calls = [call.kwargs for call in create.call_args_list]
    action = next(call for call in calls if call["extra"]["label_data"].get("sequence_kind") == "live_action")
    assert action["extra"]["label_data"]["snap_seconds"] == 5.123456789
    assert action["extra"]["label_data"]["sequence_version"] == result["result_version"]


def test_segmentation_version_invalidates_old_checkpoint_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import checkpoints
    from pipeline.film_association import ASSOCIATION_VERSION

    source = tmp_path / "source.mp4"
    source.write_bytes(b"synthetic-checkpoint-input")
    options = {"video_id": "video", "profile": "quick", "stages": ["segment"], "priority": 10}
    first = checkpoints.checkpoint_identity(source, **options)
    assert first["segmentation_version"] == ASSOCIATION_VERSION
    monkeypatch.setattr(checkpoints, "ASSOCIATION_VERSION", "next-segmentation-version")
    assert checkpoints.checkpoint_identity(source, **options)["fingerprint"] != first["fingerprint"]


@pytest.mark.parametrize("correction", [{"annotation_revision": 1}, {"human_events": [{"event_type": "snap"}]}])
def test_human_corrected_machine_layout_keeps_existing_windows_and_ids(correction: dict) -> None:
    from pipeline.orchestrator import PipelineContext, _run_video_stage

    clips = [{"id": "retained", "start_time": 2., "end_time": 10., "boundary_source": "heuristic", **correction},
             {"id": "other-active", "start_time": 12., "end_time": 20.},
             {"id": "historical", "start_time": 0., "end_time": 25., "superseded": True}]
    ctx = PipelineContext(video_id="video", input_uri="source.mp4", job_id="job", lineage={"existing_clips": clips})
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=None), patch("pipeline.stage_segment.run") as generate:
        result = _run_video_stage("segment", ctx)
    generate.assert_not_called()
    assert [clip["id"] for clip in result["clips"]] == ["retained", "other-active"]
    assert result["clips"][0]["start_time"] == 2.


def test_confirmed_alternate_view_is_eligible_shared_play_but_not_a_second_snap() -> None:
    from pipeline.stage_segment import _write_spec_clips

    first = shot("side", 0, 12, snap=3, terminal=9)
    second = shot("end", 12, 24, snap=15, terminal=21, view="endzone")
    second["association_evidence"] = {"source": "human", "kind": "alternate_angle", "target_shot_id": "side"}
    packet = associate_shots([first, second], duration=24, film_type="coach_game")
    with patch("pipeline.backend.create_clip", side_effect=lambda _v, **body: {"id": "clip", **body}) as create:
        _write_spec_clips("video", packet["clip_specs"], 24, "job", "preliminary")
    payloads = [call.kwargs["extra"] for call in create.call_args_list if call.kwargs["extra"]["label_data"].get("sequence_kind") == "live_action"]
    assert payloads[0]["play_call_id"] == payloads[1]["play_call_id"]
    assert payloads[1]["label_data"]["canonical_count_eligible"] is True
    assert payloads[1]["label_data"]["contributes_snap_count"] is False
    assert payloads[0]["capture_regime"] == "unknown"


def test_online_layout_is_collected_before_single_atomic_publish_and_errors_do_not_partially_write() -> None:
    from pipeline import backend, stage_segment
    from pipeline.video_timeline import VideoTimeline

    packet = associate_shots([shot("one", 0, 12, snap=3, terminal=9)], duration=12)
    order = []
    class Segmenter:
        source = "game_film"
        timeline = VideoTimeline((0., 3., 9.), 12., .25)
        clip_specs = packet["clip_specs"]
        def segment(self, _path: Path) -> list:
            order.append("inference")
            return []
    def snapshot(_video: str) -> list:
        order.append("snapshot")
        return [{"id": "old"}]
    with patch.object(backend, "_offline", return_value=False), patch.object(backend, "fetch_active_clips", side_effect=snapshot), patch.object(
        backend, "batch_create_clips", side_effect=RuntimeError("concurrent correction rejected")
    ) as batch, patch.object(backend, "create_clip") as single, patch.object(stage_segment, "_video_duration", return_value=12.):
        with pytest.raises(RuntimeError, match="concurrent correction"):
            stage_segment._segment("video", Path("source.mp4"), "job", Segmenter())
    assert order == ["snapshot", "inference"]
    single.assert_not_called()
    batch.assert_called_once()
    assert len(batch.call_args.args[1]) == len(packet["clip_specs"])
    assert batch.call_args.kwargs["expected_active_clip_ids"] == ["old"]


def test_batch_client_flattens_complete_clipcreate_payload_without_single_posts() -> None:
    import httpx

    from pipeline import backend

    requests = []
    def respond(request: httpx.Request) -> httpx.Response:
        import json
        requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"generation_id": "generation", "clips": [{"id": "published"}]})
    client = httpx.Client(base_url="http://worker-test", transport=httpx.MockTransport(respond))
    with patch.object(backend, "BACKEND_API_URL", "http://worker-test"), patch.object(backend, "_client", return_value=client):
        result = backend.batch_create_clips("video", [{"start_time": 0., "end_time": 12., "boundary_source": "heuristic",
            "job_id": "job", "extra": {"label_data": {"canonical_count_eligible": False}, "play_call_id": None}}],
            generation_id="generation", expected_active_clip_ids=["old"])
    assert result == [{"id": "published"}]
    assert len(requests) == 1 and requests[0][0].endswith("/clips/batch")
    body = requests[0][1]
    assert body["clips"][0]["label_data"]["canonical_count_eligible"] is False
    assert "extra" not in body["clips"][0]
    assert body["clips"][0]["provenance"]["validated"] is False


def test_overlapping_camera_shots_are_rejected_instead_of_double_counting_human_actions() -> None:
    with pytest.raises(ValueError, match="overlap"):
        associate_shots([shot("one", 0, 12, snap=3, terminal=8), shot("two", 8, 20, snap=11, terminal=16)], duration=20)


def test_invalid_event_confidence_cannot_qualify_an_action() -> None:
    source = shot("bad-confidence", 0, 12, snap=3, terminal=9)
    source["events"][0]["confidence"] = float("nan")
    result = associate_shots([source], duration=12)
    assert not any(row["canonical_count_eligible"] for row in result["sequences"])
    assert result["diagnostics"]["rejected_events"] == 1


def test_human_confirmed_additional_angle_with_ambiguous_target_abstains_from_new_count() -> None:
    first = shot("multi", 0, 24)
    first["events"] = [event("snap", 3), event("dead_ball", 8), event("snap", 16), event("dead_ball", 21)]
    alternate = shot("alternate", 24, 36, snap=27, terminal=33, view="endzone")
    alternate["association_evidence"] = {"source": "human", "kind": "alternate_angle", "target_shot_id": "multi"}
    packet = associate_shots([first, alternate], duration=36)
    row = next(row for row in actions(packet) if row["shot_id"] == "alternate")
    assert row["association_status"] == "unresolved"
    assert row["canonical_count_eligible"] is False
    assert row["canonical_view_eligible"] is False
    assert row["review_required"] is True


def test_preserved_published_machine_layout_does_not_claim_manual_provenance() -> None:
    from pipeline.orchestrator import PipelineContext, _run_video_stage

    clips = [{"id": "published-machine", "start_time": 0., "end_time": 12., "boundary_source": "heuristic"}]
    ctx = PipelineContext(video_id="video", input_uri="source.mp4", job_id="job")
    with patch("pipeline.backend.fetch_boundary_overrides", return_value=clips):
        result = _run_video_stage("segment", ctx)
    assert result["data_source"] == "preserved"
