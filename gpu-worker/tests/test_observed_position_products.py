"""Synthetic contract fixtures never qualify real football measurements."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from pipeline.observed_position_products import hydrate_position_roles, observed_position_products


def event(kind, time, **attrs):
    return {
        "id": f"{kind}-{time}",
        "event_type": kind,
        "timestamp_seconds": time,
        "source": "human",
        "attributes": {"shot_id": "one", **attrs},
    }


def track(tid, role, team, x=50, y=0, vx=0):
    return {
        "id": tid,
        "observed_role": role,
        "role_resolved": True,
        "team_label": team,
        "track_points": [
            {
                "timestamp_seconds": i / 10,
                "frame_number": i,
                "field_x": x + vx * i / 10,
                "field_y": y,
                "bbox": [1, 2, 3, 4],
                "shot_id": "one",
                "visibility_state": "observed",
            }
            for i in range(11)
        ],
    }


def participation(actor="wr", kind="routes", **attrs):
    return event(
        "position_participation",
        0,
        actor_id=actor,
        opportunity_kind=kind,
        end_seconds=1,
        attacking_direction=1,
        **attrs,
    )


def packet(rows, name):
    return next(row["metric_value"] for row in rows if row["metric_name"] == name)


def test_separation_series_and_paired_paths_are_measured_with_explicit_opportunity():
    tracks = [track("wr", "WR", "offense", vx=2), track("db", "DB", "defense", y=3)]
    rows = observed_position_products(tracks, [participation(opponent_id="db")])
    value = packet(rows, "observed_separation")
    assert value["comparison_scope"] == "explicit_comparison_defender"
    assert value["series"][0]["yards"] == 3
    assert value["series"][-1]["yards"] == pytest.approx(13**0.5)
    assert value["opportunity_kind"] == "routes"
    assert value["actor_id"] == "wr" and value["opponent_id"] == "db"
    assert len(value["paired_trajectories"]["db"]) == 11
    assert value["event_evidence"][0]["event_id"]
    assert value["definition_version"] and value["series_interpolation"] == "none"
    assert all(row["experimental_flag"] for row in rows)
    assert observed_position_products(tracks, []) == []


def test_nearest_observed_defender_is_labeled_without_coverage_responsibility():
    tracks = [track("wr", "WR", "offense"), track("db", "DB", "defense", y=3)]
    value = packet(observed_position_products(tracks, [participation()]), "observed_separation")
    assert value["comparison_scope"] == "nearest_observed_defender"
    assert value["coverage_responsibility_inferred"] is False


@pytest.mark.parametrize(
    "damage", ["hidden", "gap", "cut", "unknown_shot", "missing_pts", "unknown_visibility"]
)
def test_paired_measurements_do_not_bridge_loss_cuts_or_unknown_source_clock(damage):
    wr, db = track("wr", "WR", "offense"), track("db", "DB", "defense", y=3)
    if damage == "hidden":
        db["track_points"][4]["visibility_state"] = "occluded"
    elif damage == "gap":
        db["track_points"] = [
            p for p in db["track_points"] if p["timestamp_seconds"] not in (0.4, 0.5)
        ]
    elif damage == "cut":
        db["track_points"][4]["shot_id"] = "two"
    elif damage == "unknown_shot":
        for p in db["track_points"]:
            p.pop("shot_id")
    elif damage == "missing_pts":
        db["track_points"][4].pop("timestamp_seconds")
    else:
        db["track_points"][4].pop("visibility_state")
    rows = observed_position_products([wr, db], [participation(opponent_id="db")])
    if damage in {"unknown_shot", "missing_pts"}:
        assert rows == []
    else:
        value = packet(rows, "observed_separation")
        assert max(p["timestamp_seconds"] for p in value["series"]) <= 0.3
        assert value["right_censored"] is False
        assert value["partial_observation"] is True
        assert value["measurement_complete"] is False
        assert value["scalar_scope"] == "supported_prefix"


def test_closing_speed_is_signed_distance_derivative_and_paths_are_explicit():
    tracks = [track("edge", "EDGE", "defense", x=40, vx=2), track("qb", "QB", "offense")]
    events = [
        participation("edge", "pass_rush", opponent_id="qb"),
        event("snap", 0),
        event("first_movement", 0.2, actor_id="edge"),
    ]
    rows = observed_position_products(tracks, events)
    closing = packet(rows, "observed_closing_speed")
    assert closing["yards_per_second"] == pytest.approx(2)
    assert closing["quality_inferred"] is False
    assert packet(rows, "observed_rush_path")["yards"] == pytest.approx(2)
    assert packet(rows, "get_off_time")["seconds"] == 0.2
    assert packet(rows, "get_off_time")["event_evidence"][-1]["event_id"] == "first_movement-0.2"
    assert not any(
        r["metric_name"] == "get_off_time" for r in observed_position_products(tracks, events[:2])
    )


def test_opening_distance_preserves_negative_closing_speed():
    rows = observed_position_products(
        [track("lb", "LB", "defense", x=40, vx=-2), track("rb", "RB", "offense")],
        [participation("lb", "pursuit", opponent_id="rb")],
    )
    assert packet(rows, "observed_closing_speed")["yards_per_second"] == pytest.approx(-2)
    assert packet(rows, "observed_pursuit_path")["yards"] == pytest.approx(2)


@pytest.mark.parametrize("damage", ["role", "team", "direction", "unqualified", "opponent_team"])
def test_unresolved_or_unsupported_participation_is_unknown(damage):
    tracks = [track("wr", "WR", "offense"), track("db", "DB", "defense", y=3)]
    obs = participation(opponent_id="db")
    if damage == "role":
        tracks[0]["role_resolved"] = False
    elif damage == "team":
        tracks[0].pop("team_label")
    elif damage == "direction":
        obs["attributes"].pop("attacking_direction")
    elif damage == "unqualified":
        obs["source"] = "heuristic"
        obs["provenance"] = {"validated": True}
    else:
        tracks[1]["team_label"] = "offense"
    assert observed_position_products(tracks, [obs]) == []


def special_events():
    attrs = {"operation_id": "punt-1", "actor_id": "p", "ball_id": "ball"}
    return [
        participation("p", "punts"),
        event("snap", 0, **attrs),
        event("punt", 0.3, **attrs),
        event("landing", 0.8, field_x=95, field_y=-4, **attrs),
    ]


def test_special_teams_explicit_linked_events_produce_operation_hang_and_landing():
    tracks = [track("p", "P", "special_teams"), track("ball", "BALL", "special_teams")]
    rows = observed_position_products(tracks, special_events())
    assert packet(rows, "operation_time")["seconds"] == 0.3
    assert packet(rows, "hang_time")["seconds"] == 0.5
    landing = packet(rows, "kick_landing_location")
    assert (landing["field_x"], landing["field_y"]) == (95, -4)
    assert landing["operation_id"] == "punt-1"


def test_hang_time_needs_continuous_visible_ball_and_same_operation():
    tracks = [track("p", "P", "special_teams"), track("ball", "BALL", "special_teams")]
    tracks[1]["track_points"][5]["visibility_state"] = "lost"
    rows = observed_position_products(tracks, special_events())
    assert not any(r["metric_name"] == "hang_time" for r in rows)
    assert not any(r["metric_name"] == "kick_landing_location" for r in rows)
    events = special_events()
    events[-1]["attributes"]["operation_id"] = "other"
    rows = observed_position_products(tracks, events)
    assert [r["metric_name"] for r in rows] == ["operation_time"]


def test_te_transitions_are_explicit_states_with_interaction_and_actor_evidence():
    tracks = [track("te", "TE", "offense"), track("edge", "EDGE", "defense", y=1)]
    states = [
        event("te_usage", t, actor_id="te", interaction_id="chip-1", state=s, opponent_id="edge")
        for t, s in [(0, "block"), (0.2, "chip"), (0.4, "route")]
    ]
    rows = observed_position_products(tracks, [participation("te", "routes"), *states])
    usage = packet(rows, "te_usage_transition")
    assert [s["state"] for s in usage["series"]] == ["block", "chip", "route"]
    assert usage["seconds"] == 0.4
    assert usage["interaction_id"] == "chip-1"
    assert not any(
        r["metric_name"] == "te_usage_transition"
        for r in observed_position_products(tracks, [participation("te", "routes")])
    )
    broken = deepcopy(states)
    broken[-1]["attributes"]["interaction_id"] = "other"
    rows = observed_position_products(tracks, [participation("te", "routes"), *broken])
    assert not any(r["metric_name"] == "te_usage_transition" for r in rows)


def test_stage_hook_persists_packets_and_abstains_when_calibration_is_unsafe():
    from pipeline.stage_metrics import run

    tracks = [track("wr", "WR", "offense"), track("db", "DB", "defense", y=3)]
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "m"}):
        result = run("clip", tracks, [participation()], True, 10, "job")
        assert packet(result["metrics"], "observed_separation")["opportunity_kind"] == "routes"
        unsafe = run("clip", tracks, [participation()], False, 10, "job")
        assert not any(r["metric_name"] == "observed_separation" for r in unsafe["metrics"])


def test_source_pts_not_frame_rate_controls_irregularly_sampled_intervals():
    defender = track("lb", "LB", "defense", x=40, vx=2)
    opponent = track("rb", "RB", "offense")
    for actor in (defender, opponent):
        for point in actor["track_points"]:
            point["frame_number"] *= 90
    rows = observed_position_products(
        [defender, opponent], [participation("lb", "pursuit", opponent_id="rb")]
    )
    assert packet(rows, "observed_closing_speed")["yards_per_second"] == pytest.approx(2)


def test_endpoint_events_with_other_shots_or_unqualified_model_do_not_supply_observations():
    tracks = [track("p", "P", "special_teams"), track("ball", "BALL", "special_teams")]
    events = special_events()
    events[-1]["attributes"]["shot_id"] = "two"
    rows = observed_position_products(tracks, events)
    assert [r["metric_name"] for r in rows] == ["operation_time"]
    events = special_events()
    events[2]["source"] = "model"
    events[2]["provenance"] = {"validated": True}
    assert observed_position_products(tracks, events) == []


def test_duplicate_source_positions_conflict_and_prevent_bridging():
    wr, db = track("wr", "WR", "offense"), track("db", "DB", "defense", y=3)
    db["track_points"].append({**db["track_points"][4], "field_y": 8})
    value = packet(
        observed_position_products([wr, db], [participation(opponent_id="db")]),
        "observed_separation",
    )
    assert value["source_end_seconds"] == 0.3


def test_special_teams_cannot_substitute_kicker_role_for_punt_participation():
    tracks = [track("p", "K", "special_teams"), track("ball", "BALL", "special_teams")]
    assert observed_position_products(tracks, special_events()) == []


@pytest.mark.parametrize("analytics_safe", [True, False])
def test_opportunity_survives_missing_measurement_geometry_direction_and_end(analytics_safe):
    receiver = track("wr", "WR", "offense")
    receiver.update(player_id="resolved-player", track_points=[])
    participation_event = participation()
    participation_event["attributes"].pop("attacking_direction")
    participation_event["attributes"].pop("end_seconds")
    rows = observed_position_products(
        [receiver], [participation_event], analytics_safe=analytics_safe
    )
    assert [r["metric_name"] for r in rows] == ["position_opportunity"]
    value = packet(rows, "position_opportunity")
    assert value["value"] == 1 and value["player_id"] == "resolved-player"
    assert value["opportunity_kind"] == "routes"
    assert value["participation_event_id"] == participation_event["id"]
    assert value["source_start_seconds"] == 0
    assert value["source_end_seconds"] is None
    assert value["event_evidence"][0]["event_id"] == participation_event["id"]


@pytest.mark.parametrize("identity", [None, "unresolved", "unknown", "candidate"])
def test_opportunity_does_not_turn_unresolved_identity_into_player_rate(identity):
    receiver = track("wr", "WR", "offense")
    if identity is not None:
        receiver.update(player_id="candidate-player", identity_state=identity)
    rows = observed_position_products([receiver], [participation()])
    assert not any(r["metric_name"] == "position_opportunity" for r in rows)


def test_opportunity_requires_qualified_participation_and_explicit_role_team():
    receiver = track("wr", "WR", "offense")
    receiver["player_id"] = "resolved-player"
    assert observed_position_products([receiver], []) == []
    receiver["role_resolved"] = False
    assert observed_position_products([receiver], [participation()]) == []


def test_timing_survives_kicker_leaving_after_kick_and_unsafe_field_mapping():
    kicker = track("p", "P", "special_teams")
    for point in kicker["track_points"]:
        point.update(field_x=None, field_y=None)
        if point["timestamp_seconds"] > 0.3:
            point["visibility_state"] = "offscreen"
    rows = observed_position_products(
        [kicker, track("ball", "BALL", "special_teams")],
        special_events(),
        analytics_safe=False,
    )
    assert packet(rows, "operation_time")["seconds"] == 0.3
    assert packet(rows, "hang_time")["seconds"] == 0.5
    assert not any(r["metric_name"] == "kick_landing_location" for r in rows)


def test_duplicate_track_ids_never_choose_arbitrary_observed_actor():
    tracks = [
        track("wr", "WR", "offense"),
        track("wr", "WR", "offense", y=5),
        track("db", "DB", "defense", y=3),
    ]
    assert observed_position_products(tracks, [participation(opponent_id="db")]) == []


def test_unverified_fps_fallback_timeline_does_not_create_native_timing():
    from pipeline.video_timeline import VideoTimeline

    tracks = [track("p", "P", "special_teams"), track("ball", "BALL", "special_teams")]
    timeline = VideoTimeline(tuple(i / 10 for i in range(11)), 1.1, 10, verified=False)
    assert observed_position_products(tracks, special_events(), timeline) == []


def test_qualified_role_event_hydrates_actual_stage_inputs_without_roster_guessing():
    from pipeline.orchestrator import PipelineContext, _run_clip_stage

    receiver, defender = track("wr", "WR", "offense"), track("db", "DB", "defense", y=3)
    receiver.pop("observed_role")
    receiver.pop("role_resolved")
    receiver["player_id"] = "resolved-player"
    role = event("position_role", 0, actor_id="wr", observed_role="WR", team_label="offense")
    ctx = PipelineContext("video", "unused", analytics_safe=True)
    ctx.clip_artifacts = {
        "clip": {
            "track": {"tracklets": [receiver, defender]},
            "events": {"events": [role, participation(opponent_id="db")]},
        }
    }
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "m"}):
        result = _run_clip_stage("metrics", ctx, {"id": "clip", "start_time": 0, "end_time": 1})
    measured = packet(result["metrics"], "observed_separation")
    assert measured["actor_role"] == "WR"
    assert measured["role_evidence"][0]["event_id"] == role["id"]
    assert packet(result["metrics"], "position_opportunity")["player_id"] == "resolved-player"
    assert "observed_role" not in receiver


def test_reviewed_clip_role_annotation_is_explicit_and_does_not_assign_identity():
    actor = {"id": "wr", "position": "TE", "track_points": []}
    label = {
        "label_type": "position_roles",
        "source": "human",
        "label_value": {
            "assignments": [
                {
                    "actor_id": "wr",
                    "observed_role": "WR",
                    "team_label": "offense",
                    "player_id": "must-not-be-inferred",
                }
            ]
        },
    }
    enriched = hydrate_position_roles([actor], [], [label])
    assert enriched[0]["observed_role"] == "WR" and enriched[0]["role_resolved"]
    assert enriched[0]["team_label"] == "offense" and not enriched[0].get("player_id")
    assert not hydrate_position_roles([actor], [])[0].get("role_resolved")
    label["source"] = "model"
    assert not hydrate_position_roles([actor], [], [label])[0].get("role_resolved")


def test_conflicting_qualified_roles_remain_unknown():
    actor = track("wr", "WR", "offense")
    observations = [
        event("position_role", time, actor_id="wr", observed_role=role, team_label="offense")
        for time, role in [(0, "WR"), (0.2, "TE")]
    ]
    enriched = hydrate_position_roles([actor], observations)
    assert enriched[0]["role_resolved"] is False and enriched[0]["observed_role"] is None


def test_lost_defender_prefix_remains_geometry_evidence_without_complete_rep_claim():
    defender = track("edge", "EDGE", "defense", x=40, vx=2)
    defender["track_points"][4]["visibility_state"] = "lost"
    events = [
        participation("edge", "pass_rush", opponent_id="qb"),
        event("snap", 0),
        event("first_movement", 0.2, actor_id="edge"),
    ]
    rows = observed_position_products([defender, track("qb", "QB", "offense")], events)
    for name in ("observed_rush_path", "observed_closing_speed"):
        value = packet(rows, name)
        assert value["partial_observation"] and value["measurement_complete"] is False
        assert value["right_censored"] is False and value["scalar_scope"] == "supported_prefix"
        assert value["source_end_seconds"] == 0.3
        assert value["trajectory"][-1]["timestamp_seconds"] == 0.3
        assert value["series"] and value["scalar_summary"]
    timing = packet(rows, "get_off_time")
    assert timing["seconds"] == 0.2 and timing["measurement_complete"] is True
    assert timing["right_censored"] is False and timing["partial_observation"] is False


def test_qb_proximity_accepts_explicit_edge_pass_rush_participation_and_preserves_evidence():
    from pipeline.qb_observations import observed_qb_durations

    qb, edge = track("qb", "QB", "offense"), track("edge", "EDGE", "defense", x=40, vx=2)
    participation_event = participation("edge", "pass_rush", opponent_id="qb")
    rows = observed_qb_durations(
        [qb, edge], [event("snap", 0), event("throw", 1), participation_event], 10
    )
    value = packet(rows, "rusher_proximity")
    assert value["yards"] == 8 and value["opportunity_kind"] == "pass_rush"
    assert value["event_evidence"][-1]["event_id"] == participation_event["id"]
    assert value["pressure_inferred"] is False


def test_qb_proximity_rejects_other_shot_and_never_restarts_after_loss():
    from pipeline.qb_observations import observed_qb_durations

    qb, rusher = track("qb", "QB", "offense"), track("edge", "RUSHER", "defense", x=40)
    for point in rusher["track_points"]:
        point["shot_id"] = "different"
    events = [event("snap", 0), event("throw", 1)]
    assert not any(
        row["metric_name"] == "rusher_proximity"
        for row in observed_qb_durations([qb, rusher], events, 10)
    )
    for point in rusher["track_points"]:
        point["shot_id"] = "one"
    rusher["track_points"][4]["visibility_state"] = "lost"
    value = packet(observed_qb_durations([qb, rusher], events, 10), "rusher_proximity")
    assert value["series"][3]["yards"] == 10
    assert all(point["yards"] is None for point in value["series"][4:])
