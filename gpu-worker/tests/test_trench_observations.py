import json
from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.run_observations import observed_interactions
from pipeline.trench_observations import interaction_geometry
from pipeline.video_timeline import VideoTimeline


def fixture():
    timeline = VideoTimeline((0, .1, .2, .3), .4, 10)
    tracks = [{"id": identifier, "track_points": [
        {"frame_number": frame, "timestamp_seconds": seconds, "field_x": x + seconds,
         "field_y": 2, "visibility_state": "observed", "shot_id": "one"}
        for frame, seconds in enumerate(timeline.timestamps)
    ]} for identifier, x in [("ol", 49), ("dl", 51)]]
    return tracks, timeline


def measure(tracks, timeline, **kwargs):
    return interaction_geometry(tracks, "ol", "dl", 0, .3, "one", timeline,
                                analytics_safe=kwargs.get("analytics_safe", True))


def test_exact_pair_samples_retain_missing_actor_without_interpolation():
    tracks, timeline = fixture()
    tracks[1]["track_points"].pop(1)
    packet = measure(tracks, timeline)
    assert len(packet["samples"]) == 4
    assert packet["samples"][1]["blocker"]["field_x"] == 49.1
    assert packet["samples"][1]["defender"] is None
    assert packet["samples"][2]["defender"]["field_x"] == 51.2
    assert packet["series_interpolation"] == "none"


@pytest.mark.parametrize("change", ["hidden", "cut", "unknown_shot", "invalid_position", "interpolated", "predicted"])
def test_unobserved_actor_positions_are_barriers(change):
    tracks, timeline = fixture()
    point = tracks[1]["track_points"][1]
    point.update({"hidden": {"visibility_state": "lost"}, "cut": {"shot_id": "two"},
                  "unknown_shot": {"shot_id": "unresolved"},
                  "interpolated": {"interpolated": True}, "predicted": {"predicted": True},
                  "invalid_position": {"field_x": 111}}[change])
    assert measure(tracks, timeline)["samples"][1]["defender"] is None


@pytest.mark.parametrize("change", ["duplicate", "clock", "missing_frame", "backwards", "duplicate_actor"])
def test_ambiguous_identity_or_native_clock_refuses_geometry(change):
    tracks, timeline = fixture()
    points = tracks[0]["track_points"]
    if change == "duplicate":
        points.insert(1, deepcopy(points[0]))
    elif change == "clock":
        points[1]["timestamp_seconds"] = .11
    elif change == "missing_frame":
        points[1].pop("frame_number")
    elif change == "backwards":
        points[1], points[2] = points[2], points[1]
    else:
        tracks.append(deepcopy(tracks[0]))
    assert measure(tracks, timeline) is None


def test_temporal_duration_survives_unavailable_or_unsafe_geometry():
    tracks, timeline = fixture()
    events = [{"id": kind, "event_type": kind, "timestamp_seconds": seconds,
               "source": "human", "attributes": {"interaction_id": "pair",
               "blocker_id": "ol", "defender_id": "dl", "shot_id": "one"}}
              for kind, seconds in [("engagement_start", 0), ("engagement_end", .3)]]
    assert measure(tracks, timeline, analytics_safe=False) is None
    assert measure(tracks, VideoTimeline(timeline.timestamps, .4, 10, verified=False)) is None
    for safe in (False, True):
        rows = observed_interactions(tracks, events, timeline, analytics_safe=safe)
        assert rows[0]["metric_value"]["seconds"] == .3
        assert (rows[0]["metric_value"].get("interaction_geometry") is not None) == safe


def test_frontend_contract_contains_actual_producer_geometry():
    tracks, timeline = fixture()
    tracks[1]["track_points"].pop(1)
    path = Path(__file__).parents[2] / "frontend/src/components/__fixtures__/trench-interaction-producer.json"
    packet = json.loads(path.read_text(encoding="utf-8"))
    assert packet["source"] == "synthetic_test"
    assert packet["metric"]["metric_value"]["interaction_geometry"] == measure(tracks, timeline)


@pytest.mark.parametrize("second_clock", [0, .1])
def test_distinct_start_events_for_same_pair_refuse_duplicate_scalar_and_replay(second_clock):
    tracks, timeline = fixture()
    start = {"id": "start", "event_type": "engagement_start", "timestamp_seconds": 0,
             "source": "human", "attributes": {"interaction_id": "pair", "blocker_id": "ol",
             "defender_id": "dl", "shot_id": "one"}}
    events = [start, {**start, "id": "other-start", "timestamp_seconds": second_clock},
              {**start, "id": "end", "event_type": "engagement_end", "timestamp_seconds": .3}]
    assert observed_interactions(tracks, events, timeline, analytics_safe=True) == []
    assert observed_interactions(tracks, list(reversed(events)), timeline, analytics_safe=False) == []


@pytest.mark.parametrize("endpoint", [0, 1])
@pytest.mark.parametrize("include_valid_endpoint", [False, True])
def test_contradictory_blocker_aliases_do_not_choose_an_actor(endpoint, include_valid_endpoint):
    tracks, timeline = fixture()
    events = [{"id": kind, "event_type": kind, "timestamp_seconds": seconds,
               "source": "human", "attributes": {"interaction_id": "pair",
               "blocker_id": "ol", "defender_id": "dl", "shot_id": "one"}}
              for kind, seconds in [("engagement_start", 0), ("engagement_end", .3)]]
    conflict = deepcopy(events[endpoint])
    conflict["attributes"]["player_track_id"] = "foreign"
    if include_valid_endpoint:
        conflict["id"] = "contradictory-endpoint"
        events.append(conflict)
    else:
        events[endpoint] = conflict
    assert observed_interactions(tracks, events, timeline, analytics_safe=True) == []
