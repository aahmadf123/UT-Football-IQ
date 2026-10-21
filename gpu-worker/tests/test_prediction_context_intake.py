"""Shared UI POST contract is numeric protocol evidence, never football truth."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from pipeline.expected_outcomes.data import DataError
from pipeline.expected_outcomes.extract import extract_snapshot

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[2] / "tests_local/fixtures/prediction-context.json"
    ).read_text()
)


def tracks():
    return [
        {
            "id": identity,
            "source": "human",
            "side_of_ball": "defense" if identity in {"dl", "lb", "db"} else "offense",
            "track_points": [
                {
                    "frame_number": frame,
                    "timestamp_seconds": when,
                    "field_x": x - (0.1 if when == 12.4 else 0),
                    "field_y": y,
                    "bbox": [0, 0, 2, 4],
                    "shot_id": "native",
                    "visibility_state": "observed",
                }
                for frame, when in ((372, 12.4), (375, 12.5))
            ],
        }
        for identity, (x, y) in FIXTURE["coordinates"].items()
    ]


def saved_event(case):
    # Exact source-clock values asserted by the mounted correction POST tests.
    return {
        "id": "protocol-event",
        "source": "human",
        "event_type": case["event_type"],
        "timestamp_seconds": FIXTURE["clip_start_seconds"] + FIXTURE["prediction_play_seconds"],
        "frame_number": FIXTURE["event_frame_number"],
        "attributes": {
            **case["attributes"],
            "actor_id": case["actor_id"],
            "shot_id": "native",
            "timebase": "recording",
        },
    }


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["task"])
def test_actual_ui_payload_contract_extracts_at_native_source_clock(case):
    event = saved_event(case)
    result = extract_snapshot(
        case["task"],
        tracks(),
        [event],
        {
            "session_id": "protocol-session",
            "recording_id": "protocol-recording",
            "canonical_play_id": "protocol-play",
            "media_sha256": "a" * 64,
        },
        analytics_safe=True,
        capture_regime="fixed_sideline",
    )
    assert result["observation_clock"]["timestamp_seconds"] == 12.5
    assert (
        result["feature_evidence"]["observations"]["context"][
            "feature_context_available_at_seconds"
        ]
        == 12.4
    )
    assert "target" not in result and "outcome" not in result["feature_evidence"]
    if case["task"] == "pressure":
        assert result["features"]["blitzers"]["value"] == 0  # Explicit reviewed empty list.
        assert result["features"]["down"]["value"] == 2
    else:
        assert result["subject"]["scope"] == "player"


@pytest.mark.parametrize("fault", ["clip_clock", "missing_native_frame", "future_context"])
def test_pressure_intake_does_not_repair_unverified_clocks(fault):
    case = deepcopy(FIXTURE["cases"][-1])
    event = saved_event(case)
    if fault == "clip_clock":
        event["timestamp_seconds"] = 2.5
    elif fault == "missing_native_frame":
        event.pop("frame_number")
    else:
        event["attributes"]["feature_context_available_at_seconds"] = 12.6
    with pytest.raises(DataError):
        extract_snapshot("pressure", tracks(), [event], {}, analytics_safe=True)
