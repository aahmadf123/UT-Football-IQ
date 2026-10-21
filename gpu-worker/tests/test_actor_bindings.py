"""Exact synthetic observation bindings, never proximity or roster matching."""

from copy import deepcopy

import pytest

from pipeline.actor_binding_protocol import observation_fingerprint
from pipeline.actor_bindings import rebind_reviewed_events


def track(identity="new"):
    return {"id": identity, "clip_id": "clip", "track_points": [
        {"frame_number": i, "timestamp_seconds": i / 10, "shot_id": 0,
         "visibility_state": "observed", "bbox": [i, 0, i + 2, 4],
         "field_x": 50 + i / 10, "field_y": 2}
        for i in range(11)
    ]}


def scope():
    return {"video_id": "video", "clip_id": "clip", "start_time": 0.0, "end_time": 1.0,
            "registration_fingerprint": "a" * 64, "media_sha256": "b" * 64}


def event():
    return {"id": "observation", "event_type": "position_participation",
            "timestamp_seconds": 0, "source": "human",
            "attributes": {"actor_id": "old", "observed_role": "WR", "team_label": "offense",
                           "opportunity_kind": "routes", "end_seconds": 1, "shot_id": "0"},
            "provenance": {"source": "manual", "actor_bindings": {
                "protocol": "native-observations-v1", "scope": scope(), "references": {
                    "old": {"observation_sha256": observation_fingerprint(track()),
                            "original_track_id": "old"}}}}}


def test_exact_rebind_preserves_event_identity_and_rewrites_only_actor_fields():
    original = event()
    original["attributes"].update({"interaction_id": "old", "center_id": "old",
        "ol_roles": {"C": "old"}, "body_extents": {"old": {"min_x": 1}},
        "defender_occupancy": {"track_ids": ["old"], "complete": True}})
    saved = deepcopy(original)
    events, tracks, issues = rebind_reviewed_events([original], [track()], scope())
    assert not issues and original == saved
    assert events[0]["id"] == "observation"
    attrs = events[0]["attributes"]
    assert attrs["actor_id"] == attrs["center_id"] == attrs["ol_roles"]["C"] == "new"
    assert attrs["interaction_id"] == "old"
    assert list(attrs["body_extents"]) == ["new"]
    assert attrs["defender_occupancy"]["track_ids"] == ["new"]
    assert events[0]["provenance"]["actor_binding_lineage"]["old"] == "new"
    assert tracks[0].get("player_id") is None


def test_pressure_context_typed_references_all_rebind_without_rewriting_role_or_gap_values():
    original = event()
    original["event_type"] = "pressure_context"
    original["attributes"].update({
        "intended_receiver_id": "old", "possessor_id": "old", "opponent_tracklet_id": "old",
        "defender_track_ids": ["old"], "called_rusher_ids": ["old"],
        "called_blitzer_ids": ["old"], "protection_blocker_ids": ["old"],
        "front_track_ids": ["old"], "defensive_roles": {"old": "EDGE"},
        "dl_gap_alignment": {"old": "B"}, "concept_id": "old",
    })
    events, _, issues = rebind_reviewed_events([original], [track()], scope())
    assert not issues
    attrs = events[0]["attributes"]
    for key in ("intended_receiver_id", "possessor_id", "opponent_tracklet_id"):
        assert attrs[key] == "new"
    for key in ("defender_track_ids", "called_rusher_ids", "called_blitzer_ids",
                "protection_blocker_ids", "front_track_ids"):
        assert attrs[key] == ["new"]
    assert attrs["defensive_roles"] == {"new": "EDGE"}
    assert attrs["dl_gap_alignment"] == {"new": "B"}
    assert attrs["concept_id"] == "old"


def test_rebound_pressure_context_reaches_actual_feature_extractor():
    from pipeline.expected_outcomes.extract import extract_snapshot
    from tests.test_expected_outcomes import snapshot

    ol = {role: role.lower() for role in ("LT", "LG", "C", "RG", "RT")}
    defense = {"dt": "DT", "de": "DE", "lb": "LB"}
    coordinates = {**{tid: (49, y) for tid, y in zip(ol.values(), (6, 3, 0, -3, -6))},
                   "dt": (51, 1), "de": (51, 7), "lb": (53, 0)}
    roles = {tid: role for role, tid in ol.items()} | defense
    old_tracks = [{"id": tid, "clip_id": "clip", "source": "human", "observed_role": role,
        "role_resolved": True, "side_of_ball": "offense" if tid in ol.values() else "defense",
        "track_points": [{"frame_number": 60, "timestamp_seconds": 2.0, "shot_id": "shot",
            "visibility_state": "observed", "bbox": [index * 10, 0, index * 10 + 2, 4],
            "field_x": coordinates[tid][0], "field_y": coordinates[tid][1]}]}
        for index, (tid, role) in enumerate(roles.items())]
    attrs = {"play_design": "pass", "feature_context_available_at_seconds": 1.9,
        "ol_roles": ol, "defensive_roles": defense, "defender_track_ids": list(defense),
        "defender_population_complete": True, "front_population_complete": True,
        "call_context_known": True, "call_context_available_at_seconds": 1.8,
        "called_rusher_ids": ["dt", "de"], "called_blitzer_ids": [],
        "protection_blocker_ids": list(ol.values()), "dl_gap_alignment": {"dt": "a", "de": "edge"},
        "down": 2, "distance_yards": 7.0, "attacking_direction": 1, "los_x": 50.0, "ball_y": 0.0}
    original = {"id": "pressure-snap", "event_type": "snap", "source": "human",
        "timestamp_seconds": 2.0, "frame_number": 60, "shot_id": "shot", "attributes": attrs,
        "provenance": {"source": "manual", "actor_bindings": {
            "protocol": "native-observations-v1", "scope": scope(), "references": {
                t["id"]: {"original_track_id": t["id"], "observation_sha256": observation_fingerprint(t)}
                for t in old_tracks}}}}
    fresh = [{**deepcopy(t), "id": f"new-{i}"} for i, t in enumerate(old_tracks)]
    events, tracks, issues = rebind_reviewed_events([original], fresh, scope())
    assert not issues
    output = extract_snapshot("pressure", tracks, events, snapshot("pressure")["lineage"],
        analytics_safe=True, attacking_direction=1, down=2, distance_yards=7.0)
    assert output["features"]["dl_count"]["value"] == 2
    assert output["features"]["lb_count"]["value"] == 1
    assert output["features"]["blitzers"]["value"] == 0


@pytest.mark.parametrize("damage", ["changed", "duplicate", "missing", "clip", "video", "window", "source", "no_binding"])
def test_uncertain_rebinding_abstains_with_visible_reason(damage):
    original, tracks, current = event(), [track()], scope()
    if damage == "changed":
        tracks[0]["track_points"][-1]["bbox"][0] += 0.01
    elif damage == "duplicate":
        tracks.append(track("other"))
    elif damage == "missing":
        tracks.clear()
    elif damage in {"clip", "video"}:
        current[damage + "_id"] = "foreign"
    elif damage == "window":
        current["end_time"] = 1.1
    elif damage == "source":
        current["media_sha256"] = "c" * 64
    else:
        original["provenance"].pop("actor_bindings")
    events, _, issues = rebind_reviewed_events([original], tracks, current)
    assert events == []
    assert issues[0]["event_id"] == original["id"]
    assert issues[0]["review_required"] is True


def test_shot_zero_and_float_normalization_and_field_projection_independence():
    first = track()
    second = deepcopy(first)
    for point in second["track_points"]:
        point["shot_id"] = "0"
        point["bbox"] = [float(x) for x in point["bbox"]]
        point["field_x"] += 5
    assert observation_fingerprint(first) == observation_fingerprint(second)
    second["track_points"][0]["shot_id"] = "unknown"
    assert observation_fingerprint(second) is None


@pytest.mark.parametrize("fault", ["ol_roles", "references", "scope", "point"])
def test_malformed_actor_evidence_abstains_instead_of_crashing(fault):
    original, tracks = event(), [track()]
    if fault == "ol_roles":
        original["attributes"]["ol_roles"] = ["bad"]
    elif fault in {"references", "scope"}:
        original["provenance"]["actor_bindings"][fault] = ["bad"]
    else:
        tracks[0]["track_points"][0] = "bad"
    events, _, issues = rebind_reviewed_events([original], tracks, scope())
    assert not events and issues[0]["review_required"] is True


def test_sidecar_publication_keeps_malformed_actor_failure_without_sending_invalid_uuid(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from pipeline import backend

    sent = []

    class Client:
        def post(self, path, json):
            sent.append(json)
            return SimpleNamespace(raise_for_status=lambda: None)

    @contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(backend, "_offline", lambda: False)
    monkeypatch.setattr(backend, "_client", client)
    backend.publish_actor_binding_review("clip", "job", 3, [{"event_id": "event",
        "reason": "actor_reference_container_invalid", "review_required": True,
        "actor_ids": ["not-a-track-uuid", "00000000-0000-0000-0000-000000000001"]}])
    assert sent[0]["issues"][0]["reason"] == "actor_reference_container_invalid"
    assert sent[0]["issues"][0]["actor_ids"] == ["00000000-0000-0000-0000-000000000001"]


def test_one_to_one_binding_rejects_two_original_refs_with_same_observations():
    original = event()
    original["attributes"]["opponent_id"] = "second-old"
    original["provenance"]["actor_bindings"]["references"]["second-old"] = {
        "original_track_id": "second-old", "observation_sha256": observation_fingerprint(track())}
    assert not rebind_reviewed_events([original], [track()], scope())[0]


def test_only_server_captured_human_identity_label_proof_restores_player():
    original = event()
    reference = original["provenance"]["actor_bindings"]["references"]["old"]
    reference["manual_identity"] = {"label_id": "label", "source": "human", "annotated_by": "staff",
                                    "player_id": "player", "original_track_id": "old"}
    _, tracks, issues = rebind_reviewed_events([original], [track()], scope())
    assert not issues
    assert tracks[0]["player_id"] == "player"
    assert tracks[0]["identity_state"] == "resolved"
    reference["manual_identity"]["source"] = "model"
    assert rebind_reviewed_events([original], [track()], scope())[1][0].get("player_id") is None


@pytest.mark.parametrize("damage", [None, "changed", "duplicate"])
def test_actual_orchestrator_rebinds_before_real_metrics_producer(monkeypatch, tmp_path, damage):
    import hashlib

    from pipeline import orchestrator
    from pipeline.video_timeline import VideoTimeline

    source = tmp_path / "synthetic.mp4"
    source.write_bytes(b"synthetic-actor-contract")
    original = event()
    original["attributes"]["timebase"] = "clip"
    original["provenance"]["actor_bindings"]["scope"]["media_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    original["provenance"]["actor_bindings"]["references"]["old"]["manual_identity"] = {
        "label_id": "label", "source": "human", "annotated_by": "staff",
        "player_id": "player", "original_track_id": "old"}
    fresh = [track()]
    if damage == "changed":
        fresh[0]["track_points"][3]["bbox"][0] += 1
    elif damage == "duplicate":
        fresh.append(track("duplicate"))
    monkeypatch.setattr("pipeline.storage.download_to_temp", lambda _uri: source)
    monkeypatch.setattr("pipeline.hosted_limits.validate_master", lambda _path: None)
    monkeypatch.setattr("pipeline.video_timeline.VideoTimeline.from_path", lambda *_a, **_k: VideoTimeline(tuple(i/10 for i in range(11)), 1.1, 10))
    monkeypatch.setattr(orchestrator, "_score_clip_uncertainty", lambda *_a: None)
    monkeypatch.setattr(orchestrator, "_run_video_stage", lambda *_a: {"clips": [{"id": "clip", "start_time": 0, "end_time": 1}]})
    monkeypatch.setattr("pipeline.backend.patch_bound_manual_identity", lambda _t: None)
    monkeypatch.setattr("pipeline.backend.create_metric", lambda **_kwargs: {"id": "metric"})
    real_clip_stage = orchestrator._run_clip_stage
    observed = {}
    persisted_review = []
    monkeypatch.setattr("pipeline.backend.publish_actor_binding_review", lambda *args: persisted_review.append(args))

    def stage(name, ctx, clip):
        if name == "track":
            return {"tracklets": fresh}
        if name == "events":
            return {"events": []}
        if name == "metrics":
            observed["events"] = deepcopy(ctx.clip_artifacts["clip"]["events"])
            value = real_clip_stage(name, ctx, clip)
            observed["metrics"] = value
            return value
        return {}

    monkeypatch.setattr(orchestrator, "_run_clip_stage", stage)
    orchestrator.run_pipeline("video", "unused", stages=["segment", "track", "events", "metrics"],
        revisions={"effective_annotations": [{"id": "clip", "start_time": 0, "end_time": 1,
            "registration_fingerprint": "a" * 64, "human_events": [original]}]})
    rows = observed["metrics"]["metrics"]
    assert persisted_review and persisted_review[0][0] == "clip"
    assert bool(persisted_review[0][3]) is bool(damage)
    if damage:
        assert observed["events"]["events"] == []
        assert observed["events"]["event_review_required"][0]["review_required"] is True
        assert not any(r["metric_name"] == "position_opportunity" for r in rows)
    else:
        assert observed["events"]["events"][0]["attributes"]["actor_id"] == "new"
        opportunity = next(r for r in rows if r["metric_name"] == "position_opportunity")
        assert opportunity["metric_value"]["actor_id"] == "new"
