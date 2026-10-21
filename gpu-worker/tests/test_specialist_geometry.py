"""Explicit synthetic contract examples, never production football labels."""

from unittest.mock import patch

import pytest

from pipeline.observed_position_products import observed_position_products
from pipeline.specialist_geometry import specialist_geometry
from tests.test_observed_position_products import event, participation, track


def packet(rows, name):
    return next(row["metric_value"] for row in rows if row["metric_name"] == name)


def polygon(time, width=4, depth=3, **attrs):
    return event(
        "protection_envelope",
        time,
        envelope_id="observed-envelope",
        start_seconds=0,
        end_seconds=0.2,
        visibility_state="observed",
        polygon=[
            {"field_x": 45, "field_y": -width / 2},
            {"field_x": 45 + depth, "field_y": -width / 2},
            {"field_x": 45 + depth, "field_y": width / 2},
            {"field_x": 45, "field_y": width / 2},
        ],
        **attrs,
    )


def front(direction=1):
    tracks = [
        track(role.lower(), role, "offense", x=50, y=y * direction, vx=-direction)
        for role, y in [("LT", 4), ("LG", 2), ("C", 0), ("RG", -2), ("RT", -4)]
    ]
    snap = event(
        "snap",
        0,
        los_x=50,
        attacking_direction=direction,
        ol_roles={t["observed_role"]: t["id"] for t in tracks},
    )
    return tracks, snap


def test_explicit_polygon_dimensions_and_signed_compression_have_native_series():
    rows = specialist_geometry([], [polygon(0), polygon(0.1, width=3), polygon(0.2, width=2)])
    assert packet(rows, "protection_envelope_width")["width_yards"] == 4
    assert packet(rows, "protection_envelope_depth")["depth_yards"] == 3
    assert packet(rows, "protection_envelope_area")["area_square_yards"] == 12
    compression = packet(rows, "protection_envelope_compression")
    assert compression["square_yards_per_second"] == pytest.approx(30)
    assert compression["measurement_complete"] is True
    assert compression["series"][0]["polygon"]
    assert compression["definition_basis"] == "explicit_measured_protection_polygon"
    assert compression["protection_success_inferred"] is False
    assert all(r["experimental_flag"] for r in rows)


@pytest.mark.parametrize("damage", ["crossed", "missing_visibility", "cut", "gap", "unqualified"])
def test_polygon_unknown_or_discontinuous_geometry_never_becomes_complete(damage):
    observations = [polygon(0), polygon(0.1), polygon(0.2)]
    if damage == "crossed":
        observations[0]["attributes"]["polygon"][1:3] = observations[0]["attributes"]["polygon"][
            2:0:-1
        ]
    elif damage == "missing_visibility":
        observations[1]["attributes"].pop("visibility_state")
    elif damage == "cut":
        observations[1]["attributes"]["shot_id"] = "two"
    elif damage == "gap":
        observations[1]["timestamp_seconds"] = 0.4
        observations[2]["timestamp_seconds"] = 0.5
    else:
        for observation in observations:
            observation["source"] = "model"
            observation["provenance"] = {"validated": True}
    rows = specialist_geometry([], observations)
    if damage == "unqualified":
        assert rows == []
    else:
        assert not rows or all(row["metric_value"]["measurement_complete"] is False for row in rows)


@pytest.mark.parametrize("direction", [1, -1])
def test_original_named_first_second_and_signed_pass_set_preserve_direction(direction):
    tracks, snap = front(direction)
    pass_protection = participation("lt", "pass_protection")
    pass_protection["attributes"]["attacking_direction"] = direction
    rows = specialist_geometry(tracks, [snap, pass_protection], attacking_direction=direction)
    displacement = next(
        r["metric_value"]
        for r in rows
        if r["metric_name"] == "lineman_first_second_displacement" and r["tracklet_id"] == "lt"
    )
    assert displacement["yards"] == 1 and displacement["forward_yards"] == -1
    assert displacement["original_role"] == "LT" and displacement["measurement_complete"]
    assert packet(rows, "observed_pass_set_geometry")["depth_yards"] == 1


def test_lineman_loss_retains_partial_path_without_first_second_scalar_claim():
    tracks, snap = front()
    tracks[0]["track_points"][5]["visibility_state"] = "lost"
    rows = specialist_geometry(tracks, [snap])
    value = next(
        r["metric_value"]
        for r in rows
        if r["metric_name"] == "lineman_first_second_displacement" and r["tracklet_id"] == "lt"
    )
    assert value["source_end_seconds"] == 0.4
    assert value["partial_observation"] and not value["measurement_complete"]
    assert value["scalar_scope"] == "supported_prefix" and value["right_censored"] is False


@pytest.mark.parametrize(
    "direction,receiver_x,vx,expected", [(1, 49, 2, 1), (-1, 51, -2, 1), (1, 47, 0, -3)]
)
def test_role_qualified_route_depth_split_and_explicit_points_are_source_linked(
    direction, receiver_x, vx, expected
):
    tracks, snap = front(direction)
    receiver = track("wr", "WR", "offense", x=receiver_x, y=10 * direction, vx=vx)
    p = participation(opponent_id=None)
    p["attributes"]["attacking_direction"] = direction
    release = event(
        "receiver_release",
        0.2,
        actor_id="wr",
        field_x=receiver_x + vx * 0.2,
        field_y=10 * direction,
    )
    route_break = event(
        "route_break", 0.6, actor_id="wr", field_x=receiver_x + vx * 0.6, field_y=10 * direction
    )
    rows = specialist_geometry(
        [*tracks, receiver], [snap, p, release, route_break], attacking_direction=direction
    )
    assert packet(rows, "observed_route_depth")["yards"] == expected
    assert packet(rows, "receiver_snap_alignment")["lateral_yards"] == 10
    point = packet(rows, "receiver_release_location")
    assert point["location_event_id"] == release["id"] and point["timestamp_seconds"] == 0.2
    assert point["event_evidence"][-1]["event_type"] == "receiver_release"
    assert point["actor_id"] == "wr" and point["visibility_state"] == "observed"
    assert packet(rows, "receiver_break_location")["location_event_id"] == route_break["id"]


def test_no_route_or_location_is_inferred_from_unqualified_event_or_roster_position():
    tracks, snap = front()
    receiver = track("wr", "WR", "offense")
    receiver["role_resolved"] = False
    assert not any(
        r["metric_name"].startswith("receiver_") or r["metric_name"] == "observed_route_depth"
        for r in specialist_geometry([*tracks, receiver], [snap, participation()])
    )


@pytest.mark.parametrize(
    "role,side,kind",
    [
        ("RB", "offense", "rushes"),
        ("WR", "offense", "targets"),
        ("LT", "offense", "pass_protection"),
        ("LB", "defense", "tackle_opportunities"),
        ("KR", "special_teams", "returns"),
    ],
)
def test_extra_eligible_populations_need_no_outcome_or_geometry(role, side, kind):
    actor = track("actor", role, side)
    actor.update(player_id="resolved-player", track_points=[])
    p = participation("actor", kind)
    p["attributes"].pop("attacking_direction")
    rows = observed_position_products([actor], [p], analytics_safe=False)
    assert packet(rows, "position_opportunity")["opportunity_kind"] == kind
    actor["team_label"] = "unknown"
    assert observed_position_products([actor], [p], analytics_safe=False) == []


def test_geometry_stage_hook_is_reachable_and_calibration_gated():
    from pipeline.stage_metrics import run

    events = [polygon(0), polygon(0.1), polygon(0.2)]
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        assert (
            packet(run("clip", [], events, True, 10, "job")["metrics"], "protection_envelope_area")[
                "area_square_yards"
            ]
            == 12
        )
        assert not run("clip", [], events, False, 10, "job")["metrics"]


@pytest.mark.parametrize("sentinel", ["unknown", "unverified", "unresolved", "null"])
def test_placeholder_shot_ids_never_qualify_measured_polygons(sentinel):
    observations = [polygon(0), polygon(0.1), polygon(0.2)]
    for observation in observations:
        observation["attributes"]["shot_id"] = sentinel
    assert specialist_geometry([], observations) == []


def test_wrong_or_conflicted_original_center_cannot_supply_receiver_alignment():
    tracks, snap = front()
    receiver = track("wr", "WR", "offense", y=10)
    rb = track("rb", "RB", "offense")
    snap["attributes"]["center_id"] = "rb"
    rows = specialist_geometry([*tracks, receiver, rb], [snap, participation()])
    assert not any(row["metric_name"] == "receiver_snap_alignment" for row in rows)


def test_explicit_receiver_point_survives_unrelated_route_loss_without_snap_geometry():
    receiver = track("wr", "WR", "offense")
    receiver["track_points"][2]["visibility_state"] = "lost"
    location = event("route_break", 0.6, actor_id="wr", field_x=51, field_y=2)
    rows = specialist_geometry([receiver], [participation(), location])
    value = packet(rows, "receiver_break_location")
    assert value["location_event_id"] == location["id"] and value["measurement_complete"]
    location["attributes"]["actor_id"] = "other"
    assert specialist_geometry([receiver], [participation(), location]) == []


def test_structured_snap_original_map_hydrates_roles_without_roster_inference():
    from pipeline.observed_position_products import hydrate_position_roles

    tracks, snap = front()
    for actor in tracks:
        actor.pop("observed_role")
        actor.pop("role_resolved")
        actor["team_label"] = "unknown"
    enriched = hydrate_position_roles(tracks, [snap])
    assert {actor.get("observed_role") for actor in enriched} == {"LT", "LG", "C", "RG", "RT"}
    assert all(
        actor.get("role_resolved") is True and actor["team_label"] == "offense"
        for actor in enriched
    )
    assert packet(specialist_geometry(enriched, [snap]), "lineman_first_second_displacement")[
        "measurement_complete"
    ]


@pytest.mark.parametrize("shot", [0, 1])
def test_fresh_numeric_detection_shots_match_staff_string_shots_through_real_tracker(shot):
    from pipeline.observed_position_products import hydrate_position_roles
    from pipeline.stage_track import run

    detections = {
        str(index): [
            {
                "bbox": [49, -2, 51, 0],
                "confidence": 1,
                "timestamp_seconds": index / 10,
                "timestamp_verified": True,
                "shot_id": shot,
                "source": "synthetic_test",
            },
            {
                "bbox": [49, 8, 51, 10],
                "confidence": 1,
                "timestamp_seconds": index / 10,
                "timestamp_verified": True,
                "shot_id": shot,
                "source": "synthetic_test",
            },
        ]
        for index in range(11)
    }
    with patch(
        "pipeline.stage_track.backend.create_tracklet", side_effect=[{"id": "wr"}, {"id": "db"}]
    ):
        produced = run(
            "clip",
            detections,
            10,
            "job",
            variant="iou-tracker",
            analytics_safe=True,
            homography=[1, 0, 0, 0, 1, 0, 0, 0, 1],
        )["tracklets"]
    assert len(produced) == 2 and all(
        point["shot_id"] == str(shot) for actor in produced for point in actor["track_points"]
    )
    roles = [
        event(
            "position_role", 0, actor_id=tid, observed_role=role, team_label=side, shot_id=str(shot)
        )
        for tid, role, side in [("wr", "WR", "offense"), ("db", "DB", "defense")]
    ]
    participation_event = participation(opponent_id="db", shot_id=str(shot))
    rows = observed_position_products(
        hydrate_position_roles(produced, roles), [participation_event]
    )
    assert packet(rows, "observed_separation")["yards"] == 10


@pytest.mark.parametrize("other_shot", ["two", None, "unknown"])
def test_interaction_duration_never_crosses_cut_or_unknown_shot(other_shot):
    from pipeline.run_observations import observed_interactions

    observations = [
        event("engagement_start", 0, blocker_id="ol", defender_id="dl", interaction_id="pair"),
        event("engagement_end", 0.2, blocker_id="ol", defender_id="dl", interaction_id="pair"),
    ]
    observations[-1]["attributes"]["shot_id"] = other_shot
    assert observed_interactions([{"id": "ol"}, {"id": "dl"}], observations) == []
