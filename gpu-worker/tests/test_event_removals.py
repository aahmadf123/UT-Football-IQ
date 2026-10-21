"""Reviewed removals are exact negative evidence, never fuzzy clock matching."""

from copy import deepcopy

import pytest

from pipeline.actor_binding_protocol import observation_fingerprint
from pipeline.event_removals import suppress_removed_proposals
from pipeline.video_timeline import VideoTimeline
from tests.test_actor_bindings import scope, track


def original():
    return {
        "id": "removed",
        "event_type": "snap",
        "timestamp_seconds": 0.2,
        "attributes": {"actor_id": "old", "shot_id": "0", "timebase": "clip"},
        "provenance": {"source": "manual"},
    }


def tombstone():
    event = original()
    return {
        **event,
        "provenance": {
            "state": "superseded",
            "removal": {
                "correction_id": "server-proof",
                "source": "human",
                "original_event": event,
                "actor_bindings": {
                    "protocol": "native-observations-v1",
                    "scope": scope(),
                    "references": {
                        "old": {
                            "original_track_id": "old",
                            "observation_sha256": observation_fingerprint(track()),
                        }
                    },
                },
            },
        },
    }


def proposal():
    return {
        "id": "new-model",
        "event_type": "snap",
        "source": "model",
        "frame_number": 2,
        "timestamp_seconds": 0.2,
        "attributes": {"actor_id": "new", "shot_id": 0, "timebase": "recording"},
    }


TIMELINE = VideoTimeline(tuple(i / 10 for i in range(11)), 1.1, 10)


def test_exact_native_proposal_removal_preserves_other_humans_and_unrelated_actor_events():
    saved = tombstone()
    human = {**proposal(), "id": "other-human", "source": "human"}
    other = deepcopy(proposal())
    other["attributes"]["actor_id"] = "different"
    kept, issues = suppress_removed_proposals(
        [proposal(), human, other], [saved], [track()], scope(), TIMELINE
    )
    assert kept == [human, other] and not issues
    assert saved == tombstone()


@pytest.mark.parametrize(
    "fault",
    [
        "clock",
        "frame",
        "shot",
        "unknown_shot",
        "contradictory_shot",
        "source",
        "clip",
        "window",
        "bbox",
        "duplicate",
        "attributes",
        "proof",
    ],
)
def test_missing_or_changed_identity_never_suppresses_nearby_new_event(fault):
    saved, current, tracks, current_scope = tombstone(), proposal(), [track()], scope()
    if fault == "clock":
        current["timestamp_seconds"] = 0.20000001
    elif fault == "frame":
        current["frame_number"] = 3
    elif fault == "shot":
        current["attributes"]["shot_id"] = "other"
    elif fault == "unknown_shot":
        current["attributes"]["shot_id"] = "unknown"
    elif fault == "contradictory_shot":
        current["shot_id"] = "conflicting-shot"
    elif fault == "source":
        current_scope["media_sha256"] = "c" * 64
    elif fault == "clip":
        current_scope["clip_id"] = "other"
    elif fault == "window":
        current_scope["end_time"] = 1.1
    elif fault == "bbox":
        tracks[0]["track_points"][2]["bbox"][0] += 0.1
    elif fault == "duplicate":
        tracks.append(track("duplicate"))
    elif fault == "attributes":
        current["attributes"]["measured_x"] = 5
    elif fault == "proof":
        saved["provenance"]["removal"]["actor_bindings"]["scope"]["media_sha256"] = None
    kept, _ = suppress_removed_proposals([current], [saved], tracks, current_scope, TIMELINE)
    assert kept == [current]


def test_actual_removed_id_still_requires_exact_known_native_clock_and_source_scope():
    saved, current = tombstone(), proposal()
    current["id"] = "removed"
    assert suppress_removed_proposals([current], [saved], [track()], scope(), TIMELINE)[0] == []
    assert suppress_removed_proposals([current], [saved], [], scope(), TIMELINE)[0] == [current]
    current["timestamp_seconds"] = 0.3
    current["frame_number"] = 3
    assert suppress_removed_proposals([current], [saved], [], scope(), TIMELINE)[0] == [current]


@pytest.mark.parametrize("fault", ["actor", "attributes", "bbox", "duplicate"])
def test_reused_removed_id_never_overrides_changed_or_ambiguous_observations(fault):
    saved, current, tracks = tombstone(), proposal(), [track()]
    current["id"] = "removed"
    if fault == "actor":
        current["attributes"]["actor_id"] = "different"
    elif fault == "attributes":
        current["attributes"]["measured_x"] = 6
    elif fault == "bbox":
        tracks[0]["track_points"][2]["bbox"][0] += 1
    else:
        tracks.append(track("duplicate"))
    assert suppress_removed_proposals([current], [saved], tracks, scope(), TIMELINE)[0] == [current]


@pytest.mark.parametrize("changed", [False, True])
def test_actual_stage_dispatch_filters_before_persistence_and_retains_native_metadata(
    monkeypatch, changed
):
    from pipeline import orchestrator, stage_events

    current = proposal()
    current["attributes"].pop("shot_id")
    monkeypatch.setattr(
        stage_events, "_detect_multisignal", lambda *_a, **_k: ([deepcopy(current)], {})
    )
    written = []
    monkeypatch.setattr(
        stage_events.backend,
        "create_event",
        lambda *a, **k: written.append((a, k)) or {"id": "persisted-id"},
    )
    fresh = track()
    if changed:
        fresh["track_points"][2]["bbox"][0] += 1
    ctx = orchestrator.PipelineContext(
        video_id="video", input_uri="unused", job_id="new-job", priority=10
    )
    ctx.fps, ctx.timeline = 10, TIMELINE
    ctx.detections = {i: [{"bbox": [0, 0, 2, 4], "shot_id": 0}] for i in range(11)}
    ctx.lineage = {"source_sha256": "b" * 64}
    ctx.effective_annotations = {"clip": {**scope(), "removed_events": [tombstone()]}}
    ctx.clip_artifacts = {"clip": {"track": {"tracklets": [fresh]}}}
    result = orchestrator._run_clip_stage(
        "events", ctx, {"id": "clip", "start_time": 0, "end_time": 1}
    )
    assert bool(written) is changed
    assert result["event_count"] == int(changed)
    if changed:
        assert result["events"][0]["id"] == "persisted-id"
        assert result["events"][0]["attributes"]["shot_id"] == "0"
        assert result["events"][0]["attributes"]["timebase"] == "recording"
        assert result["event_removal_review_required"]
