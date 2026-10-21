"""Reviewed interval observations survive orchestration and share its clock."""

import pytest

from pipeline.orchestrator import _merge_reviewed_event, _normalize_interval_clock


def test_multiple_interval_observations_survive_and_retry_replaces_only_its_id():
    events = [{"id": "auto", "event_type": "pressure_observation", "source": "heuristic"}]
    for identity, start in (("early", 0), ("late", 2), ("early", 0.1)):
        _merge_reviewed_event(
            events,
            {
                "id": identity,
                "event_type": "pressure_observation",
                "source": "human",
                "timestamp_seconds": start,
            },
        )
    assert [(e["id"], e["timestamp_seconds"]) for e in events] == [("late", 2), ("early", 0.1)]
    _merge_reviewed_event(events, {"id": "snap-1", "event_type": "snap", "source": "human"})
    _merge_reviewed_event(events, {"id": "snap-2", "event_type": "snap", "source": "human"})
    assert [e["id"] for e in events if e["event_type"] == "snap"] == ["snap-1", "snap-2"]


def test_clip_interval_boundaries_are_converted_once_to_recording_clock():
    event = {"attributes": {"timebase": "clip", "start_seconds": 0.5, "end_seconds": 2}}
    assert _normalize_interval_clock(event, "clip", 10)
    assert event["attributes"] == {
        "timebase": "clip",
        "interval_timebase": "recording",
        "start_seconds": 10.5,
        "end_seconds": 12,
    }
    assert _normalize_interval_clock(event, "recording", 10)
    assert event["attributes"]["start_seconds"] == 10.5
    invalid = {
        "attributes": {"interval_timebase": "unknown", "start_seconds": 0.5, "end_seconds": 2}
    }
    assert not _normalize_interval_clock(invalid, "recording", 10)


@pytest.mark.parametrize(
    "kind",
    [
        "position_participation",
        "position_role",
        "te_usage",
        "first_movement",
        "kick",
        "punt",
        "landing",
        "protection_envelope",
        "receiver_release",
        "route_break",
    ],
)
def test_distinct_position_actor_observations_survive_and_retry_replaces_only_its_id(kind):
    events = []
    for identity, actor, time in [
        ("first", "one", 0),
        ("second", "two", 0.2),
        ("first", "one", 0.1),
    ]:
        _merge_reviewed_event(
            events,
            {
                "id": identity,
                "event_type": kind,
                "source": "human",
                "timestamp_seconds": time,
                "attributes": {"actor_id": actor},
            },
        )
    assert [(e["id"], e["timestamp_seconds"]) for e in events] == [("second", 0.2), ("first", 0.1)]


def test_participation_event_clock_supplies_interval_start_and_offset_once():
    event = {
        "event_type": "position_participation",
        "timestamp_seconds": 0.5,
        "attributes": {"timebase": "clip", "end_seconds": 2},
    }
    assert _normalize_interval_clock(event, "clip", 10)
    assert event["attributes"]["start_seconds"] == 10.5
    assert event["attributes"]["end_seconds"] == 12
    assert _normalize_interval_clock(event, "recording", 10)
    assert event["attributes"]["start_seconds"] == 10.5


def test_unknown_timebase_discards_only_corresponding_multi_actor_observation():
    from pipeline.orchestrator import _discard_reviewed_event

    events = [
        {"id": key, "event_type": "position_participation", "source": "human"}
        for key in ("one", "two")
    ]
    _discard_reviewed_event(events, {"id": "one", "event_type": "position_participation"})
    assert [e["id"] for e in events] == ["two"]


def test_actual_reviewed_accept_path_preserves_actors_te_states_and_native_interval(
    monkeypatch, tmp_path
):
    from pipeline.orchestrator import run_pipeline
    from pipeline.video_timeline import VideoTimeline

    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"synthetic-test-only")
    monkeypatch.setattr("pipeline.storage.download_to_temp", lambda _uri: source)
    monkeypatch.setattr("pipeline.hosted_limits.validate_master", lambda _path: None)
    monkeypatch.setattr(
        "pipeline.video_timeline.VideoTimeline.from_path",
        lambda *_args, **_kwargs: VideoTimeline(tuple(i / 10 for i in range(121)), 12.1, 10),
    )
    monkeypatch.setattr("pipeline.orchestrator._score_clip_uncertainty", lambda *_args: None)
    monkeypatch.setattr(
        "pipeline.orchestrator._run_video_stage",
        lambda *_args: {"clips": [{"id": "clip", "start_time": 10, "end_time": 11}]},
    )

    captured = []
    import hashlib

    from pipeline.actor_binding_protocol import actor_references, observation_fingerprint

    tracks = [
        {
            "id": name,
            "clip_id": "clip",
            "track_points": [
                {
                    "frame_number": i,
                    "timestamp_seconds": i / 10,
                    "shot_id": 0,
                    "visibility_state": "observed",
                    "bbox": [index * 10, 0, index * 10 + 2, 4],
                }
                for i in range(100, 111)
            ],
        }
        for index, name in enumerate(("one", "two", "three"))
    ]
    scope = {
        "video_id": "video",
        "clip_id": "clip",
        "start_time": 10,
        "end_time": 11,
        "registration_fingerprint": "a" * 64,
        "media_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }

    def clip_stage(stage, ctx, clip):
        if stage == "track":
            return {"tracklets": tracks}
        if stage == "events":
            return {
                "events": [
                    {
                        "id": "approved-machine-snap",
                        "event_type": "snap",
                        "source": "model",
                        "provenance": {
                            "validated": True,
                            "approved_evaluation_id": "approved",
                            "artifact_sha256": "a" * 64,
                            "quality_checks_passed": True,
                        },
                    }
                ]
            }
        if stage == "metrics":
            captured.extend(ctx.clip_artifacts[clip["id"]]["events"]["events"])
            from pipeline.expected_outcomes.data import DataError
            from pipeline.expected_outcomes.extract import extract_snapshot

            with pytest.raises(DataError, match="correction remains unresolved"):
                extract_snapshot("pressure", tracks, captured, {}, analytics_safe=True)
        return {}

    monkeypatch.setattr("pipeline.orchestrator._run_clip_stage", clip_stage)

    def human(identity, kind, when, **attrs):
        return {
            "id": identity,
            "event_type": kind,
            "timestamp_seconds": when,
            "attributes": {"timebase": "clip", **attrs},
        }

    events = [
        {**human("invalid-staff-snap", "snap", 0), "attributes": {"timebase": "unknown"}},
        human("route-one", "position_participation", 0, actor_id="one", end_seconds=1),
        human("route-two", "position_participation", 0, actor_id="two", end_seconds=1),
        human("te-block", "te_usage", 0, actor_id="one", state="block"),
        human("te-chip", "te_usage", 0.2, actor_id="one", state="chip"),
        human("te-route", "te_usage", 0.4, actor_id="one", state="route"),
        human(
            "polygon-one",
            "protection_envelope",
            0,
            envelope_id="pocket",
            start_seconds=0,
            end_seconds=0.2,
        ),
        human(
            "polygon-two",
            "protection_envelope",
            0.2,
            envelope_id="pocket",
            start_seconds=0,
            end_seconds=0.2,
        ),
        human("release-one", "receiver_release", 0.2, actor_id="one"),
        human("release-two", "receiver_release", 0.3, actor_id="two"),
        human("break-one", "route_break", 0.5, actor_id="one"),
        human("break-two", "route_break", 0.7, actor_id="two"),
        {
            **human("invalid-other", "position_participation", 0, actor_id="three", end_seconds=1),
            "attributes": {"timebase": "unknown", "actor_id": "three", "end_seconds": 1},
        },
    ]
    for item in events:
        item["provenance"] = {
            "source": "manual",
            "actor_bindings": {
                "protocol": "native-observations-v1",
                "scope": scope,
                "references": {
                    old: {
                        "original_track_id": old,
                        "observation_sha256": observation_fingerprint(
                            next(t for t in tracks if t["id"] == old)
                        ),
                    }
                    for old in actor_references(item["attributes"])
                },
            },
        }
    run_pipeline(
        "video",
        "unused",
        stages=["segment", "track", "events", "metrics"],
        revisions={
            "effective_annotations": [
                {
                    "id": "clip",
                    "start_time": 10,
                    "end_time": 11,
                    "human_events": events,
                    "registration_fingerprint": "a" * 64,
                }
            ]
        },
    )
    assert {
        e["id"] for e in captured if e.get("event_type") != "prediction_clock_review_required"
    } == {
        "approved-machine-snap",
        "route-one",
        "route-two",
        "te-block",
        "te-chip",
        "te-route",
        "polygon-one",
        "polygon-two",
        "release-one",
        "release-two",
        "break-one",
        "break-two",
    }
    route = next(e for e in captured if e["id"] == "route-two")
    assert route["timestamp_seconds"] == 10
    assert route["attributes"]["start_seconds"] == 10
    assert route["attributes"]["end_seconds"] == 11
