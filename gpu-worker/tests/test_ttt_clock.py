"""Ambiguous or disputed reviewed clocks never become qualified TTT."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from pipeline.stage_metrics import run
from pipeline.video_timeline import VideoTimeline


def event(kind, when, **attrs):
    return {
        "id": f"{kind}-{when}",
        "event_type": kind,
        "timestamp_seconds": when,
        "frame_number": round(when * 10),
        "source": "human",
        "attributes": {"timebase": "recording", "shot_id": 0, **attrs},
    }


def timing(events, timeline=None):
    with patch("pipeline.stage_metrics.backend.create_metric", return_value={"id": "metric"}):
        rows = run("clip", [], events, False, 10, "job", timeline=timeline)["metrics"]
    return next((row for row in rows if row["metric_name"] == "time_to_throw"), None)


@pytest.mark.parametrize(
    "fault", ["snap_duplicate", "release_alias", "conflicting_id", "snap_review", "release_review"]
)
def test_disputed_or_multiple_reviewed_clocks_do_not_choose_a_duration(fault):
    events = [event("snap", 0), event("pass_release", 2)]
    if fault == "snap_duplicate":
        events.append(event("snap", 0.2))
    if fault == "release_alias":
        events.append(event("throw", 1.5))
    if fault == "conflicting_id":
        conflict = deepcopy(events[1])
        conflict["timestamp_seconds"] = 1.5
        events.append(conflict)
    if fault in {"snap_review", "release_review"}:
        events.append(
            {
                "event_type": "prediction_clock_review_required",
                "attributes": {
                    "clock_family": "snap" if fault == "snap_review" else "pass_release"
                },
            }
        )
    assert timing(events) is None


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "shot",
        "unknown_clock",
        "conflicting_clock",
        "unknown_shot",
        "unverified_timeline",
        "missing_clock",
        "conflicting_shot",
        "zero_duration",
    ],
)
def test_only_supported_human_clocks_qualify_without_tracking_or_calibration(fault):
    events = [event("snap", 0), event("pass_release", 2)]
    timeline = None
    if fault == "shot":
        events[1]["attributes"]["shot_id"] = "other"
    if fault == "unknown_clock":
        events[0]["attributes"]["timebase"] = "unknown"
    if fault == "conflicting_clock":
        events[0]["provenance"] = {"timebase": "clip"}
    if fault == "unknown_shot":
        events[0]["attributes"]["shot_id"] = "unknown"
    if fault == "unverified_timeline":
        timeline = VideoTimeline(tuple(i / 10 for i in range(31)), 3.1, 10, verified=False)
    if fault == "missing_clock":
        events[0]["attributes"].pop("timebase")
        events[1]["attributes"].pop("timebase")
    if fault == "conflicting_shot":
        events[0]["provenance"] = {"shot_id": "other"}
    if fault == "zero_duration":
        events[1]["timestamp_seconds"] = events[1]["frame_number"] = 0
    row = timing(events, timeline)
    assert row is not None
    assert bool(row.get("analytics_safe")) is (fault is None)


def test_one_reviewed_clock_takes_precedence_over_an_automatic_alias():
    events = [
        event("snap", 0),
        event("pass_release", 2),
        {**event("throw", 1), "source": "heuristic"},
    ]
    row = timing(events)
    assert row["analytics_safe"] and row["metric_value"]["seconds"] == 2


def test_mixed_explicit_clocks_use_actual_nonzero_native_pts():
    snap = event("snap", 1)
    snap["attributes"]["timebase"] = "clip"
    snap["frame_number"] = 110
    release = event("forward_pass_release", 13)
    timeline = VideoTimeline(tuple(i / 10 for i in range(141)), 14.1, 10, verified=True)
    row = timing([snap, release], timeline)
    assert row["analytics_safe"] and row["metric_value"]["seconds"] == 2
    assert [point["timestamp_seconds"] for point in row["metric_value"]["event_evidence"]] == [
        11,
        13,
    ]


def test_clip_clock_offsets_and_native_frame_times_must_agree():
    snap = event("snap", 1, timebase="clip")
    snap["frame_number"] = 110
    release = event("throw", 2, timebase="clip")
    release["frame_number"] = 130
    timeline = VideoTimeline(tuple(i / 10 for i in range(141)), 14.1, 10, verified=True)
    assert not timing([snap, release], timeline).get("analytics_safe")


def test_missing_manual_timestamp_does_not_qualify_through_native_frame_fallback():
    events = [event("snap", 0), event("throw", 2)]
    events[1]["timestamp_seconds"] = None
    timeline = VideoTimeline(tuple(i / 10 for i in range(31)), 3.1, 10, verified=True)
    assert not timing(events, timeline).get("analytics_safe")
