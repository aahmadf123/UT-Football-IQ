"""Observed football geometry requires source evidence, never proximity labels."""

from unittest.mock import patch

import pytest

from pipeline.position_evidence import sample_at
from pipeline.qb_observations import observed_qb_durations
from pipeline.run_observations import observed_interactions
from pipeline.stage_metrics import run as metrics_run
from pipeline.stage_oline import run as oline_run


def event(kind, time, **attributes):
    return {
        "id": f"{kind}-{time}",
        "event_type": kind,
        "timestamp_seconds": time,
        "frame_number": round(time * 10),
        "source": "human",
        "attributes": attributes,
    }


def point(time, x=47, y=0, **extra):
    return {
        "frame_number": round(time * 10),
        "timestamp_seconds": time,
        "field_x": x,
        "field_y": y,
        "bbox": [1, 2, 3, 4],
        "shot_id": "shot-1",
        "visibility_state": "observed",
        **extra,
    }


def quarterback(points=None):
    return {
        "id": "qb",
        "observed_role": "QB",
        "role_resolved": True,
        "track_points": points or [point(i / 10) for i in range(31)],
    }


def coverage(start, end, state="none", **attrs):
    return event(
        "pressure_observation",
        start,
        start_seconds=start,
        end_seconds=end,
        pressure_state=state,
        coverage_complete=True,
        qb_id="qb",
        **attrs,
    )


def metric(rows, name):
    return next(row["metric_value"] for row in rows if row["metric_name"] == name)


def front():
    roles = {"LT": ("lt", 4), "LG": ("lg", 2), "C": ("c", 0), "RG": ("rg", -2), "RT": ("rt", -4)}
    tracks = [
        {
            "id": tid,
            "observed_role": role,
            "role_resolved": True,
            "team_label": "offense",
            "track_points": [point(i / 10, 50, y) for i in range(31)],
        }
        for role, (tid, y) in roles.items()
    ]
    bodies = {
        tid: {"min_x": 49.8, "max_x": 50.2, "min_y": y - 0.4, "max_y": y + 0.4}
        for tid, y in roles.values()
    }
    snap = event(
        "snap",
        0,
        los_x=50,
        attacking_direction=1,
        ol_roles={role: tid for role, (tid, _y) in roles.items()},
        body_extents=bodies,
        defender_occupancy={"complete": True, "track_ids": []},
    )
    return tracks, snap


def test_no_pressure_is_censored_only_with_qualified_continuous_negative_coverage():
    rows = observed_qb_durations(
        [quarterback()], [event("snap", 0), coverage(0, 3), event("throw", 2)], 10
    )
    measured = metric(rows, "time_to_first_pressure")
    assert measured["seconds"] == 2 and measured["right_censored"]
    assert measured["endpoint"] == "throw"
    assert measured["event_evidence"] and measured["pressure_coverage"]
    assert not any(
        row["metric_name"] == "time_to_first_pressure"
        for row in observed_qb_durations([quarterback()], [event("snap", 0), event("throw", 2)], 10)
    )


def test_early_pressure_survives_retreat_and_stops_at_release():
    rows = observed_qb_durations(
        [quarterback()],
        [
            event("snap", 0),
            coverage(0, 0.8),
            coverage(0.8, 1.1, "pressure"),
            coverage(1.1, 3),
            event("throw", 2),
        ],
        10,
    )
    measured = metric(rows, "time_to_first_pressure")
    assert measured["seconds"] == 0.8 and not measured["right_censored"]


def test_pressure_coverage_hole_censors_before_later_pressure():
    rows = observed_qb_durations(
        [quarterback()],
        [event("snap", 0), coverage(0, 0.7), coverage(1, 2, "pressure"), event("throw", 2.5)],
        10,
    )
    measured = metric(rows, "time_to_first_pressure")
    assert measured["seconds"] == 0.7 and measured["right_censored"]
    assert measured["endpoint"] == "pressure_coverage_gap"


def test_validated_boolean_or_distance_does_not_qualify_pressure_observation():
    guessed = coverage(0, 2)
    guessed["source"] = "proximity_heuristic"
    guessed["provenance"] = {"validated": True}
    rows = observed_qb_durations(
        [quarterback()], [event("snap", 0), guessed, event("throw", 2)], 10
    )
    assert all(row["metric_name"] != "time_to_first_pressure" for row in rows)


@pytest.mark.parametrize(
    "release,expected,endpoint", [(2.5, 1.5, "sustained_exit"), (1.6, 1.6, "throw")]
)
def test_tackle_corridor_exit_requires_two_hundred_continuous_ms(release, expected, endpoint):
    tracks, snap = front()
    points = [point(i / 10, 47, 5 if i in (10, 11) or i >= 15 else 0) for i in range(31)]
    rows = observed_qb_durations(
        [*tracks, quarterback(points)], [snap, event("throw", release)], 10
    )
    measured = metric(rows, "tackle_box_residence")
    assert measured["seconds"] == expected and measured["endpoint"] == endpoint
    assert measured["corridor"]["initial_tackle_ids"] == ["lt", "rt"]
    if endpoint == "sustained_exit":
        assert measured["exit_confirmed_seconds"] == 1.7


def test_tackle_corridor_abstains_without_actual_tackle_extents():
    tracks, snap = front()
    snap["attributes"].pop("body_extents")
    rows = observed_qb_durations([*tracks, quarterback()], [snap, event("throw", 2)], 10)
    assert all(row["metric_name"] != "tackle_box_residence" for row in rows)


def test_clear_lane_subtracts_body_extents_and_defender_occupancy_at_los():
    tracks, snap = front()
    snap["attributes"]["body_extents"]["dl"] = {
        "min_x": 49.8,
        "max_x": 50.2,
        "min_y": 0.9,
        "max_y": 1.1,
    }
    snap["attributes"]["defender_occupancy"]["track_ids"] = ["dl"]
    tracks.append({"id": "dl", "team_label": "defense", "track_points": [point(0, 50, 1)]})
    with patch("pipeline.stage_oline.backend.create_metric", return_value={"id": "metric"}):
        result = oline_run("clip", tracks, [snap], True, 10, "job")
    lane = next(
        row["metric_value"]
        for row in result["metrics"]
        if row["metric_name"] == "clear_lane_width" and row["metric_value"]["gap_label"] == "left_A"
    )
    assert lane["width_yards"] == pytest.approx(0.5)
    assert len(lane["clear_intervals"]) == 2
    assert lane["measurement_region"]["los_x"] == 50
    assert all(row["experimental_flag"] for row in result["metrics"])


@pytest.mark.parametrize("missing", ["body_extents", "defender_occupancy"])
def test_clear_lane_abstains_when_actual_occupancy_is_unknown(missing):
    tracks, snap = front()
    snap["attributes"].pop(missing)
    with patch("pipeline.stage_oline.backend.create_metric", return_value={"id": "metric"}):
        result = oline_run("clip", tracks, [snap], True, 10, "job")
    assert not any(row["metric_name"] == "clear_lane_width" for row in result["metrics"])


@pytest.mark.parametrize("direction,end_x,expected", [(1, 48, -2), (-1, 52, -2)])
def test_signed_gain_requires_identified_carrier_and_preserves_negative_yards(
    direction, end_x, expected
):
    tracks, snap = front()
    snap["attributes"]["attacking_direction"] = direction
    runner = {
        "id": "rb",
        "observed_role": "RB",
        "role_resolved": True,
        "track_points": [point(i / 10, end_x, 1) for i in range(31)],
    }
    events = [
        snap,
        event("handoff", 0.2, carrier_id="rb"),
        event("end_of_play", 2, carrier_id="rb"),
    ]
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        result = metrics_run(
            "clip", [*tracks, runner], events, True, 10, "job", attacking_direction=direction
        )
    gain = metric(result["metrics"], "signed_forward_gain")
    assert gain["yards"] == expected and gain["track_id"] == "rb"
    assert gain["event_evidence"] and gain["trajectory"]


def test_actual_entry_gap_uses_original_roles_and_source_clock_crossing():
    tracks, snap = front()
    runner = {
        "id": "rb",
        "observed_role": "RB",
        "role_resolved": True,
        "track_points": [point(i / 10, 49 + i / 10, 1) for i in range(31)],
    }
    events = [
        snap,
        event("handoff", 0.2, carrier_id="rb"),
        event("end_of_play", 2, carrier_id="rb"),
    ]
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        result = metrics_run(
            "clip", [*tracks, runner], events, True, 10, "job", attacking_direction=1
        )
    entry = metric(result["metrics"], "actual_entry_gap")
    assert entry["gap_label"] == "left_A" and entry["entry_seconds"] == 1
    assert metric(result["metrics"], "handoff_to_los")["seconds"] == 0.8


def test_event_interaction_requires_same_resolved_defender_not_just_actor():
    tracks = [{"id": tid} for tid in ("ol", "dl", "different")]
    events = [
        event(
            "engagement_start",
            1,
            interaction_id="eng",
            blocker_id="ol",
            defender_id="dl",
            shot_id="one",
        ),
        event(
            "engagement_end",
            2,
            interaction_id="eng",
            blocker_id="ol",
            defender_id="different",
            shot_id="one",
        ),
    ]
    assert observed_interactions(tracks, events) == []
    events[1]["attributes"]["defender_id"] = "dl"
    measured = metric(observed_interactions(tracks, events), "engagement_duration")
    assert measured["seconds"] == 1 and measured["defender_id"] == "dl"
    assert observed_interactions([], events) == []


def test_chip_route_delay_requires_pair_evidence():
    tracks = [{"id": "te"}]
    events = [
        event("chip_end", 0.7, interaction_id="chip", blocker_id="te", shot_id="one"),
        event("route_start", 1.2, interaction_id="chip", player_track_id="te", shot_id="one"),
    ]
    assert metric(observed_interactions(tracks, events), "chip_to_route_delay")["seconds"] == 0.5
    events[1]["source"] = "proximity_heuristic"
    assert observed_interactions(tracks, events) == []


@pytest.mark.parametrize("barrier", ["lost", "cut", "gap"])
def test_qb_continuous_metrics_stop_at_barrier_and_never_use_later_release(barrier):
    points = [point(i / 10) for i in range(31)]
    if barrier == "lost":
        points[10]["visibility_state"] = "lost"
    elif barrier == "cut":
        for p in points[10:]:
            p["shot_id"] = "shot-2"
    else:
        points = points[:10] + points[14:]
    rows = observed_qb_durations(
        [quarterback(points)], [event("snap", 0), coverage(0, 3), event("throw", 2)], 10
    )
    holding = metric(rows, "ball_holding_duration")
    assert holding["seconds"] == 0.9 and holding["right_censored"]
    assert metric(rows, "time_to_first_pressure")["seconds"] == 0.9


def test_real_proximity_curve_does_not_create_pressure():
    rusher = {
        "id": "rusher",
        "observed_role": "RUSHER",
        "role_resolved": True,
        "track_points": [point(i / 10, 47.1, 0) for i in range(31)],
    }
    rows = observed_qb_durations([quarterback(), rusher], [event("snap", 0), event("throw", 2)], 10)
    proximity = metric(rows, "rusher_proximity")
    assert proximity["yards"] == pytest.approx(0.1) and not proximity["pressure_inferred"]
    assert all(row["metric_name"] != "time_to_first_pressure" for row in rows)


def test_clear_lane_complete_claim_must_include_known_defender():
    tracks, snap = front()
    tracks.append({"id": "omitted", "team_label": "defense", "track_points": [point(0, 50, 1)]})
    with patch("pipeline.stage_oline.backend.create_metric", return_value={"id": "metric"}):
        result = oline_run("clip", tracks, [snap], True, 10, "job")
    assert not any(row["metric_name"] == "clear_lane_width" for row in result["metrics"])


def test_lane_names_stay_original_when_blockers_pull_and_width_is_not_sum_of_intervals():
    tracks, snap = front()
    later = event("geometry_observation", 1, **snap["attributes"])
    later["attributes"]["body_extents"] = {
        **snap["attributes"]["body_extents"],
        "lg": {"min_x": 52, "max_x": 53, "min_y": -3, "max_y": -2},
    }
    with patch("pipeline.stage_oline.backend.create_metric", return_value={"id": "metric"}):
        rows = oline_run("clip", tracks, [snap, later], True, 10, "job")["metrics"]
    lane = next(
        r["metric_value"]
        for r in rows
        if r["metric_name"] == "clear_lane_width" and r["metric_value"]["gap_label"] == "left_A"
    )
    assert lane["initial_pair"] == ["lg", "c"]
    assert lane["series"][1]["width_yards"] == pytest.approx(1.6)
    assert lane["series_interpolation"] == "none"


def test_signed_gain_abstains_without_possession_and_cannot_choose_most_moving_track():
    tracks, snap = front()
    runner = {"id": "fast", "track_points": [point(i / 10, 49 + i, 0) for i in range(31)]}
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = metrics_run(
            "clip", [*tracks, runner], [snap, event("end_of_play", 2)], True, 10, "job"
        )["metrics"]
    assert not any(r["metric_name"] in {"signed_forward_gain", "actual_entry_gap"} for r in rows)


def test_scramble_initiation_does_not_end_corridor_or_pressure_observation():
    tracks, snap = front()
    qb = quarterback([point(i / 10, 47, 5 if i >= 15 else 0) for i in range(31)])
    rows = observed_qb_durations(
        [*tracks, qb],
        [
            snap,
            event("scramble_start", 0.5),
            coverage(0, 1),
            coverage(1, 2, "pressure"),
            event("throw", 2.5),
        ],
        10,
    )
    assert metric(rows, "ball_holding_duration")["seconds"] == 0.5
    assert metric(rows, "tackle_box_residence")["seconds"] == 1.5
    assert metric(rows, "time_to_first_pressure")["seconds"] == 1


def test_pending_and_superseded_pressure_evidence_cannot_establish_coverage():
    for state in ("pending", "superseded"):
        interval = coverage(0, 3)
        interval["provenance"] = {"publication_state": state}
        rows = observed_qb_durations(
            [quarterback()], [event("snap", 0), interval, event("throw", 2)], 10
        )
        assert not any(r["metric_name"] == "time_to_first_pressure" for r in rows)


def test_entry_can_be_observed_without_known_terminal_gain():
    tracks, snap = front()
    runner = {"id": "rb", "track_points": [point(i / 10, 49 + i / 10, 1) for i in range(31)]}
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = metrics_run(
            "clip",
            [*tracks, runner],
            [snap, event("handoff", 0.2, carrier_id="rb")],
            True,
            10,
            "job",
        )["metrics"]
    assert metric(rows, "handoff_to_los")["seconds"] == 0.8
    assert metric(rows, "actual_entry_gap")["gap_label"] == "left_A"
    assert not any(r["metric_name"] == "signed_forward_gain" for r in rows)


def test_generic_contact_is_not_first_contact_without_explicit_evidence():
    tracks, snap = front()
    runner = {"id": "rb", "track_points": [point(i / 10, 49 + i / 10, 1) for i in range(31)]}
    contact = event("contact", 1.5, carrier_id="rb")
    events = [
        snap,
        event("handoff", 0.2, carrier_id="rb"),
        contact,
        event("end_of_play", 2, carrier_id="rb"),
    ]
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = metrics_run("clip", [*tracks, runner], events, True, 10, "job")["metrics"]
        assert not any(r["metric_name"] == "pre_contact_yards" for r in rows)
        contact["attributes"]["first_contact"] = True
        rows = metrics_run("clip", [*tracks, runner], events, True, 10, "job")["metrics"]
    assert metric(rows, "pre_contact_yards")["yards"] == 0.5
    assert metric(rows, "post_contact_yards")["yards"] == 0.5


@pytest.mark.parametrize("invalid", ["shot_missing", "outside_field"])
def test_qualified_continuity_rejects_unknown_shot_and_out_of_field_coordinates(invalid):
    points = [point(i / 10) for i in range(31)]
    for p in points:
        if invalid == "shot_missing":
            p.pop("shot_id")
        else:
            p["field_y"] = 100
            p.pop("bbox")
    assert (
        observed_qb_durations([quarterback(points)], [event("snap", 0), event("throw", 2)], 10)
        == []
    )


def test_unsafe_calibration_never_emits_spatial_qb_products():
    tracks, snap = front()
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = metrics_run(
            "clip", [*tracks, quarterback()], [snap, event("throw", 2)], False, 10, "job"
        )["metrics"]
    assert metric(rows, "ball_holding_duration")["seconds"] == 2
    assert not any(r["metric_name"] in {"tackle_box_residence", "rusher_proximity"} for r in rows)


def test_explicit_pass_release_populates_existing_ttt_and_preserves_event_evidence():
    released = event("pass_release", 2, shot_id="shot-1")
    released.pop("source")
    released["attributes"]["source"] = "human"
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = metrics_run("clip", [quarterback()], [event("snap", 0), released], True, 10, "job")[
            "metrics"
        ]
    assert metric(rows, "time_to_throw")["seconds"] == 2
    release_evidence = metric(rows, "ball_holding_duration")["event_evidence"][-1]
    assert release_evidence["source"] == "human" and release_evidence["shot_id"] == "shot-1"


def test_named_gap_mirrors_field_coordinates_without_renaming_original_roles():
    tracks, snap = front()
    snap["attributes"]["attacking_direction"] = -1
    for track in tracks:
        for p in track["track_points"]:
            p["field_y"] *= -1
    for bounds in snap["attributes"]["body_extents"].values():
        bounds["min_y"], bounds["max_y"] = -bounds["max_y"], -bounds["min_y"]
    runner = {"id": "rb", "track_points": [point(i / 10, 51 - i / 10, -1) for i in range(31)]}
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = metrics_run(
            "clip",
            [*tracks, runner],
            [snap, event("handoff", 0.2, carrier_id="rb")],
            True,
            10,
            "job",
            attacking_direction=-1,
        )["metrics"]
    assert metric(rows, "actual_entry_gap")["gap_label"] == "left_A"
    assert metric(rows, "handoff_to_los")["source_end_seconds"] == 1


@pytest.mark.parametrize("los,direction", [(999, 1), (50, -1)])
def test_carrier_measurements_reject_impossible_los_or_conflicting_orientation(los, direction):
    tracks, snap = front()
    snap["attributes"]["los_x"] = los
    runner = {"id": "rb", "track_points": [point(i / 10, 49 + i / 10, 1) for i in range(31)]}
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = metrics_run(
            "clip",
            [*tracks, runner],
            [
                snap,
                event("handoff", 0.2, carrier_id="rb"),
                event("end_of_play", 2, carrier_id="rb"),
            ],
            True,
            10,
            "job",
            attacking_direction=direction,
        )["metrics"]
    assert not any(
        r["metric_name"] in {"signed_forward_gain", "handoff_to_los", "actual_entry_gap"}
        for r in rows
    )


@pytest.mark.parametrize("conflict", ["hidden", "cut", "coordinates"])
def test_conflicting_same_timestamp_samples_block_exact_and_interpolated_support(conflict):
    other = point(0.1)
    if conflict == "hidden":
        other["visibility_state"] = "lost"
    elif conflict == "cut":
        other["shot_id"] = "other"
    else:
        other["field_x"] += 1
    track = {"track_points": [point(0), point(0.1), other, point(0.2)]}
    assert sample_at(track, 0.1) is None
    assert sample_at(track, 0.05) is None
    assert sample_at(track, 0.15) is None


def test_pressure_onset_at_release_censors_without_zero_length_coverage():
    rows = observed_qb_durations(
        [quarterback()],
        [event("snap", 0), coverage(0, 2), coverage(2, 3, "pressure"), event("throw", 2)],
        10,
    )
    observed = metric(rows, "time_to_first_pressure")
    assert observed["seconds"] == 2 and observed["right_censored"]
    assert all(c["end_seconds"] > c["start_seconds"] for c in observed["pressure_coverage"])


def test_unclocked_lost_marker_cannot_be_discarded_to_bridge_a_trajectory():
    points = [point(i / 10) for i in range(31)]
    points.insert(10, {"frame_number": 10, "visibility_state": "lost", "shot_id": "shot-1"})
    assert (
        observed_qb_durations([quarterback(points)], [event("snap", 0), event("throw", 2)], 10)
        == []
    )
