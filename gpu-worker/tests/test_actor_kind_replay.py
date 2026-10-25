"""Staff person kinds follow exact saved observations, never color or proximity."""

from copy import deepcopy

import pytest

from pipeline.actor_binding_protocol import PROTOCOL, observation_fingerprint
from pipeline.actor_kind_replay import replay_actor_kinds


def track(identity="new", kind="player"):
    return {
        "id": identity,
        "clip_id": "clip",
        "class": kind,
        "team_label": kind if kind != "player" else "home",
        "player_id": None,
        "provenance": {"actor_kind": kind},
        "track_points": [
            {
                "frame_number": i,
                "timestamp_seconds": i / 10,
                "shot_id": 0,
                "visibility_state": "observed",
                "bbox": [i, 0, i + 2, 4],
            }
            for i in range(11)
        ],
    }


def scope():
    return {
        "video_id": "video",
        "clip_id": "clip",
        "start_time": 0,
        "end_time": 1,
        "registration_fingerprint": "a" * 64,
        "media_sha256": "b" * 64,
    }


def label(kind="official", **overrides):
    return {
        "id": "staff-label",
        "clip_id": "clip",
        "tracklet_id": "old",
        "source": "human",
        "annotated_by": "staff",
        "created_at": "2026-10-21T16:00:00Z",
        "label_type": "player_identity",
        "label_value": {
            "actor_kind": kind,
            "actor_bindings": {
                "protocol": PROTOCOL,
                "scope": scope(),
                "references": {
                    "old": {
                        "original_track_id": "old",
                        "observation_sha256": observation_fingerprint(track()),
                    }
                },
            },
        },
        **overrides,
    }


@pytest.mark.parametrize("kind", ["official", "sideline", "unknown"])
def test_manual_nonplayer_kind_leaves_player_analysis_and_never_inherits_roster_identity(kind):
    current = track()
    current.update(
        player_id="accidental-roster", position_group="QB", observed_role="QB", role_resolved=True
    )
    saved = deepcopy(current)
    players, nonplayers, issues = replay_actor_kinds([label(kind)], [current], [], scope())
    assert not players and not issues and current == saved
    assert nonplayers[0]["class"] == nonplayers[0]["provenance"]["actor_kind"] == kind
    assert nonplayers[0]["player_id"] is None
    assert not nonplayers[0].get("role_resolved") and not nonplayers[0].get("position_group")
    proof = nonplayers[0]["provenance"]["actor_classification"]
    assert proof["method"] == "staff_review" and proof["validated"] is True
    assert proof["inherited_from"] == "staff-label"
    assert proof["actor_binding_lineage"] == {"old": "new"}


def test_staff_player_correction_releases_exact_official_candidate_without_inventing_identity():
    players, nonplayers, issues = replay_actor_kinds(
        [label("player")], [], [track(kind="official")], scope()
    )
    assert not nonplayers and not issues
    assert players[0]["class"] == "player" and players[0]["team_label"] == "unknown"
    assert players[0].get("player_id") is None and not players[0].get("observed_role")


def test_last_created_staff_kind_wins_independent_of_snapshot_order():
    latest = label("player", id="latest", source="manual", created_at="2026-10-21T17:00:00+00:00")
    for labels in ([latest, label()], [label(), latest]):
        players, other, issues = replay_actor_kinds(labels, [], [track(kind="official")], scope())
        assert len(players) == 1 and not other and not issues
        assert players[0]["provenance"]["actor_classification"]["inherited_from"] == "latest"


def test_unapproved_model_kind_cannot_override_person_kind():
    players, other, issues = replay_actor_kinds(
        [label("player", source="model")], [], [track(kind="official")], scope()
    )
    assert not players and not issues and other[0]["class"] == "official"


def test_unknown_team_without_a_person_kind_is_not_a_nonplayer_classification():
    current = track()
    current.pop("class")
    current["provenance"] = {}
    current["team_label"] = "unknown"
    players, others, issues = replay_actor_kinds([], [current], [], scope())
    assert len(players) == 1 and not others and not issues


@pytest.mark.parametrize(
    "change",
    [
        "missing_binding",
        "changed_geometry",
        "changed_clock",
        "unknown_shot",
        "duplicate",
        "foreign_source",
        "foreign_clip",
        "foreign_window",
        "no_annotator",
        "same_time_conflict",
    ],
)
def test_unresolved_staff_kind_holds_current_player_analysis_without_guessing(change):
    entry, tracks, expected_scope = label(), [track()], scope()
    if change == "missing_binding":
        entry["label_value"].pop("actor_bindings")
    if change == "changed_geometry":
        tracks[0]["track_points"][0]["bbox"][0] += 0.1
    if change == "changed_clock":
        tracks[0]["track_points"][0]["timestamp_seconds"] = 0.001
    if change == "unknown_shot":
        tracks[0]["track_points"][0]["shot_id"] = "unknown"
    if change == "duplicate":
        tracks.append(track("duplicate"))
    if change == "foreign_source":
        expected_scope["media_sha256"] = "c" * 64
    if change == "foreign_clip":
        expected_scope["clip_id"] = "foreign"
    if change == "foreign_window":
        expected_scope["start_time"] = 0.1
    if change == "no_annotator":
        entry["annotated_by"] = None
    entries = (
        [entry, label("player", id="conflicting")] if change == "same_time_conflict" else [entry]
    )
    players, others, issues = replay_actor_kinds(entries, tracks, [], expected_scope)
    assert not players and len(others) == len(tracks) and issues
    assert all(t["provenance"]["actor_kind"] == "unknown" for t in others)
    assert all(issue["review_required"] is True for issue in issues)


def test_numeric_zero_and_string_zero_shot_ids_match_exactly():
    current = track()
    for point in current["track_points"]:
        point["shot_id"] = "0"
    players, others, issues = replay_actor_kinds([label()], [current], [], scope())
    assert not players and len(others) == 1 and not issues


def test_inherited_effective_label_uses_original_binding_actor_and_root_review_label():
    entry = label(
        "official",
        id="successor-label",
        source="inherited_human",
        tracklet_id="published-successor",
    )
    root_id = "7a298116-c608-479e-a354-c9ff2b603c7b"
    entry["label_value"].update(
        inherited_from="2bd4e044-ea10-418d-aae9-883620902f16", actor_review_origin_label_id=root_id
    )
    players, others, issues = replay_actor_kinds([entry], [track()], [], scope())
    assert not players and not issues
    proof = others[0]["provenance"]["actor_classification"]
    assert proof["inherited_from"] == root_id and proof["replayed_label_id"] == "successor-label"
    assert proof["actor_binding_lineage"] == {"old": "new"}


def test_inherited_without_root_origin_cannot_invent_staff_authority():
    entry = label("player", source="inherited_human")
    entry["label_value"]["inherited_from"] = "unproved-parent"
    players, others, issues = replay_actor_kinds([entry], [track()], [], scope())
    assert not players and issues and others[0]["class"] == "unknown"


@pytest.mark.parametrize("mode", ["official", "manual_player", "held"])
def test_actual_fresh_and_cached_track_accept_replay_precedes_player_consumers(
    monkeypatch, tmp_path, mode
):
    import hashlib

    from pipeline.orchestrator import DirArtifactSink, run_pipeline
    from pipeline.video_timeline import VideoTimeline

    source = tmp_path / "test.mp4"
    source.write_bytes(b"synthetic-clock-fixture-only")
    monkeypatch.setattr("pipeline.storage.download_to_temp", lambda _uri: source)
    monkeypatch.setattr("pipeline.hosted_limits.validate_master", lambda _path: None)
    monkeypatch.setattr(
        "pipeline.video_timeline.VideoTimeline.from_path",
        lambda *_args, **_kwargs: VideoTimeline(tuple(i / 10 for i in range(11)), 1.1, 10),
    )
    monkeypatch.setattr("pipeline.orchestrator._score_clip_uncertainty", lambda *_args: None)
    monkeypatch.setattr(
        "pipeline.orchestrator._run_video_stage",
        lambda *_args: {"clips": [{"id": "clip", "start_time": 0, "end_time": 1}]},
    )
    expected_scope = {**scope(), "media_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    entry = identity_label() if mode == "manual_player" else label()
    entry["label_value"]["actor_bindings"]["scope"] = expected_scope
    raw = {
        "tracklets": [] if mode == "manual_player" else [track()],
        "non_player_tracklets": [track(kind="official")] if mode == "manual_player" else [],
    }
    if mode == "held":
        entry["label_value"].pop("actor_bindings")
    consumers = []
    order = []
    monkeypatch.setattr(
        "pipeline.backend.patch_bound_manual_identity",
        lambda bound: order.append(("identity_patch", bound["player_id"])),
    )

    def stage(stage_name, ctx, clip):
        if stage_name == "track":
            return deepcopy(raw)
        state = ctx.clip_artifacts[clip["id"]]["track"]
        consumers.append((stage_name, deepcopy(state)))
        order.append((stage_name, None))
        if mode == "manual_player":
            assert state["non_player_tracklets"] == []
            assert state["tracklets"][0]["player_id"] == entry["label_value"]["player_id"]
            assert order[0][0] == "identity_patch"
        else:
            assert state["tracklets"] == []
            assert state["non_player_tracklets"][0]["class"] == (
                "unknown" if mode == "held" else "official"
            )
        return {}

    monkeypatch.setattr("pipeline.orchestrator._run_clip_stage", stage)
    sink = DirArtifactSink(tmp_path / "artifacts")
    args = {
        "stages": ["segment", "track", "reid", "metrics"],
        "artifact_sink": sink,
        "revisions": {
            "effective_annotations": [
                {
                    "id": "clip",
                    "start_time": 0,
                    "end_time": 1,
                    "registration_fingerprint": expected_scope["registration_fingerprint"],
                    "actor_kind_labels": [entry],
                }
            ]
        },
    }
    first = run_pipeline("video", str(source), **args)
    assert first["failed_clip_stages"] == [] and [name for name, _ in consumers] == [
        "reid",
        "metrics",
    ]
    assert bool(first["clips"][0]["actor_kind_review_required"]) == (mode == "held")
    # Replay a raw cached track before fresh consumers; this fixture's mock
    # download recreates the worker-owned temp source after normal cleanup.
    sink.write("track", raw, "clip")
    source.write_bytes(b"synthetic-clock-fixture-only")
    from pipeline.orchestrator import NullArtifactSink

    class TrackCache(NullArtifactSink):
        def load(self, stage_name, clip_id=None):
            return sink.load(stage_name, clip_id) if stage_name in {"segment", "track"} else None

        def configure(self, identity):
            sink.configure(identity)

    consumers.clear()
    order.clear()
    second = run_pipeline("video", str(source), **{**args, "artifact_sink": TrackCache()})
    assert second["failed_clip_stages"] == [] and [name for name, _ in consumers] == [
        "reid",
        "metrics",
    ]
    assert second["stage_states"]["track:clip"]["resumed"] is True


def test_staff_player_kind_uses_only_explicit_coherent_unit_evidence():
    entry = label("player")
    entry["label_value"]["team"] = "offense"
    players, _, issues = replay_actor_kinds([entry], [], [track(kind="official")], scope())
    assert not issues and players[0]["team_label"] == "offense"
    assert players[0]["provenance"]["unit_source"] == "staff_review"
    entry["label_value"]["team_label"] = "defense"
    players, others, issues = replay_actor_kinds([entry], [track()], [], scope())
    assert not players and issues and others[0]["class"] == "unknown"


def identity_label():
    entry = label(
        "player",
        id="7a298116-c608-479e-a354-c9ff2b603c7b",
        annotated_by="2bd4e044-ea10-418d-aae9-883620902f16",
    )
    entry["label_value"]["player_id"] = "5c03551b-908d-44ce-a37b-6612d071d1df"
    return entry


def test_explicit_human_roster_identity_survives_official_to_player_after_exact_binding():
    entry = identity_label()
    players, _, issues = replay_actor_kinds([entry], [], [track(kind="official")], scope())
    assert not issues and players[0]["player_id"] == entry["label_value"]["player_id"]
    claim = players[0]["provenance"]["manual_identity_binding"]
    assert claim["label_id"] == entry["id"] and claim["annotated_by"] == entry["annotated_by"]
    assert claim["original_track_id"] == "old" and claim["current_track_id"] == "new"


def test_inherited_kind_uses_separate_bound_manual_identity_origin_never_kind_root():
    entry = label("player", source="inherited_human", tracklet_id="successor")
    root = "7a298116-c608-479e-a354-c9ff2b603c7b"
    identity_root = "2bd4e044-ea10-418d-aae9-883620902f16"
    entry["label_value"].update(inherited_from=identity_root, actor_review_origin_label_id=root)
    identity = {
        "source": "human",
        "label_id": identity_root,
        "annotated_by": root,
        "player_id": "5c03551b-908d-44ce-a37b-6612d071d1df",
        "original_track_id": "old",
    }
    entry["label_value"]["actor_bindings"]["references"]["old"]["manual_identity"] = identity
    players, _, issues = replay_actor_kinds([entry], [], [track(kind="official")], scope())
    assert not issues and players[0]["player_id"] == identity["player_id"]
    assert players[0]["provenance"]["manual_identity_binding"]["label_id"] == identity_root
    assert players[0]["provenance"]["actor_classification"]["inherited_from"] == root


def test_explicit_roster_unassignment_overrules_older_bound_identity():
    entry = identity_label()
    entry["label_value"]["actor_bindings"]["references"]["old"]["manual_identity"] = {
        "source": "human",
        "label_id": entry["id"],
        "annotated_by": entry["annotated_by"],
        "player_id": entry["label_value"]["player_id"],
        "original_track_id": "old",
    }
    entry["label_value"]["player_id"] = None
    players, _, issues = replay_actor_kinds([entry], [track()], [], scope())
    assert not issues and players[0]["player_id"] is None
    assert not players[0]["provenance"].get("manual_identity_binding")
