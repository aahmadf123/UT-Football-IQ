"""Actual measured body snapshots exercise lane timing, not blocking success."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from pipeline.lane_windows import lane_windows


def fixture():
    roles = {
        "LT": ("lt", 4.0),
        "LG": ("lg", 2.0),
        "C": ("c", 0.0),
        "RG": ("rg", -2.0),
        "RT": ("rt", -4.0),
    }

    def point(t, x, y):
        return {
            "timestamp_seconds": t,
            "frame_number": round(t * 10),
            "field_x": x,
            "field_y": y,
            "shot_id": "shot",
            "visibility_state": "observed",
        }

    tracks = [
        {
            "id": identity,
            "source": "human",
            "observed_role": role,
            "role_resolved": True,
            "team_label": "offense",
            "track_points": [point(i / 10, 50.0, y) for i in range(5)],
        }
        for role, (identity, y) in roles.items()
    ]
    tracks += [
        {
            "id": "defender",
            "source": "human",
            "team_label": "defense",
            "track_points": [point(i / 10, 50.0, 1.0) for i in range(5)],
        },
        {
            "id": "runner",
            "source": "human",
            "team_label": "offense",
            "track_points": [point(i / 10, 49.8 + i / 10, 1.0) for i in range(5)],
        },
    ]
    bodies = {
        identity: {"min_x": 49.8, "max_x": 50.2, "min_y": y - 0.4, "max_y": y + 0.4}
        for identity, y in roles.values()
    }
    events = []
    for index, opened in enumerate((False, True, True, False, True)):
        footprint = {
            **bodies,
            "defender": {
                "min_x": 51 if opened else 49.8,
                "max_x": 51.4 if opened else 50.2,
                "min_y": 0.0,
                "max_y": 2.0,
            },
        }
        events.append(
            {
                "id": f"geometry-{index}",
                "event_type": "snap" if index == 0 else "geometry_observation",
                "source": "human",
                "timestamp_seconds": index / 10,
                "frame_number": index,
                "shot_id": "shot",
                "attributes": {
                    "los_x": 50.0,
                    "attacking_direction": 1,
                    "ol_roles": {role: identity for role, (identity, _) in roles.items()},
                    "body_extents": footprint,
                    "defender_occupancy": {"complete": True, "track_ids": ["defender"]},
                },
            }
        )
    events += [
        {
            "id": "handoff",
            "event_type": "handoff",
            "timestamp_seconds": 0.1,
            "frame_number": 1,
            "shot_id": "shot",
            "source": "human",
            "attributes": {"carrier_id": "runner"},
        }
    ]
    return tracks, events


def lane(rows, name="gap_clear_timeline"):
    return next(
        row["metric_value"]
        for row in rows
        if row["metric_name"] == name and row["metric_value"]["gap_label"] == "left_A"
    )


def test_measured_lane_changes_are_intervals_and_event_widths_are_exact():
    tracks, events = fixture()
    rows = lane_windows(tracks, events, attacking_direction=1)
    packet = lane(rows)
    assert packet["opening_threshold_yards"] == 0
    assert packet["series_interpolation"] == "none"
    assert packet["openings"][0] == {
        "lower_seconds": 0.0,
        "upper_seconds": 0.1,
        "left_censored": False,
    }
    assert packet["closures"][0] == {
        "lower_seconds": 0.2,
        "upper_seconds": 0.3,
        "right_censored": False,
    }
    assert packet["closures"][-1] == {
        "lower_seconds": 0.4,
        "upper_seconds": None,
        "right_censored": True,
    }
    assert lane(rows, "clear_lane_width_at_handoff")["width_yards"] == pytest.approx(1.2)
    assert lane(rows, "clear_lane_width_at_entry")["width_yards"] == pytest.approx(1.2)
    assert lane(rows, "clear_lane_width_at_entry")["alignment_seconds"] == 0.2


@pytest.mark.parametrize(
    "damage", ["missing_snapshot", "unknown_occupancy", "cut", "source", "duplicate_clock"]
)
def test_unknown_samples_do_not_become_zero_or_continuous_opening(damage):
    tracks, events = fixture()
    if damage == "missing_snapshot":
        events.pop(1)
    elif damage == "unknown_occupancy":
        events[1]["attributes"]["defender_occupancy"]["complete"] = False
    elif damage == "cut":
        events[1]["shot_id"] = "other"
    elif damage == "source":
        events[1]["provenance"] = {"source": "synthetic_test"}
    else:
        conflict = deepcopy(events[1])
        conflict["id"] = "conflicting-geometry"
        conflict["attributes"]["body_extents"]["defender"]["min_x"] = 49.8
        events.insert(2, conflict)
    rows = lane_windows(tracks, events, attacking_direction=1)
    assert not any(row["metric_name"] == "clear_lane_width_at_handoff" for row in rows)
    assert lane(rows)["openings"][0]["left_censored"] is (damage != "missing_snapshot")


def test_aligned_width_never_interpolates_occupancy_and_no_guessed_opening_threshold():
    tracks, events = fixture()
    events[-1]["timestamp_seconds"] = 0.15
    rows = lane_windows(tracks, events, attacking_direction=1)
    assert not any(row["metric_name"] == "clear_lane_width_at_handoff" for row in rows)
    assert lane_windows(tracks, events, attacking_direction=1, threshold_yards=True) == []


def test_existing_oline_stage_reaches_measured_lane_windows():
    from pipeline.stage_oline import run

    tracks, events = fixture()
    with patch("pipeline.stage_oline.backend.create_metric", return_value={"id": "metric"}):
        output = run("clip", tracks, events, True, 10, "job", attacking_direction=1)
    assert lane(output["metrics"])["measurement_region"]["cross_section"] == "LOS"
    assert all(row["experimental_flag"] for row in output["metrics"])


def test_unverified_clock_direction_and_source_samples_stay_unavailable():
    from types import SimpleNamespace

    tracks, events = fixture()
    assert lane_windows(tracks, events, SimpleNamespace(verified=False), 1) == []
    events[0]["shot_id"] = "unverified"
    assert lane_windows(tracks, events, attacking_direction=1) == []
    tracks, events = fixture()
    tracks[0]["provenance"] = {"source": "synthetic_test"}
    rows = lane_windows(tracks, events, attacking_direction=1)
    assert not any(row["metric_value"]["gap_label"] == "left_B" for row in rows)


def test_future_geometry_cannot_supply_event_width_and_unknown_clock_has_no_change_time():
    tracks, events = fixture()
    events[1]["timestamp_seconds"] = 0.11
    rows = lane_windows(tracks, events, attacking_direction=1)
    assert not any(row["metric_name"] == "clear_lane_width_at_handoff" for row in rows)
    tracks, events = fixture()
    unknown = deepcopy(events[1])
    unknown["id"] = "unknown-clock"
    unknown.pop("timestamp_seconds")
    unknown.pop("frame_number")
    events.append(unknown)
    packet = lane(lane_windows(tracks, events, attacking_direction=1))
    assert packet["unknown_clock_observations"] == 1
    assert packet["openings"] == packet["closures"] == []


@pytest.mark.parametrize("side", [None, "defense"])
def test_unknown_or_contradictory_carrier_side_has_no_event_alignment(side):
    tracks, events = fixture()
    tracks[-1].pop("team_label")
    if side:
        tracks[-1]["team_label"] = "offense"
        tracks[-1]["side_of_ball"] = side
    rows = lane_windows(tracks, events, attacking_direction=1)
    assert not any(row["metric_name"].startswith("clear_lane_width_at_") for row in rows)


def test_conflicting_explicit_carrier_aliases_cannot_align_to_another_actor():
    tracks, events = fixture()
    events[-1]["attributes"]["actor_id"] = "lt"
    rows = lane_windows(tracks, events, attacking_direction=1)
    assert not any(row["metric_name"].startswith("clear_lane_width_at_") for row in rows)
