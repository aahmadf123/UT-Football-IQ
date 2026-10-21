"""Synthetic protocol only: actual staff-save API -> independent worker producer."""

import json
import subprocess
from pathlib import Path

import pytest
from test_clip_edits import film, workspace  # noqa: F401 -- shared rollback fixture


@pytest.mark.parametrize("source_time", [9.9, 11.1])
async def test_reviewed_recording_clock_cannot_escape_its_source_window(workspace, source_time):  # noqa: F811
    client, db, session = workspace
    _, (clip,) = await film(db, session, [(10, 11)])
    response = await client.post(
        "/api/v1/corrections",
        json={
            "clip_id": str(clip.id),
            "correction_type": "event_tag",
            "corrected_value": {
                "event_type": "pass_release",
                "timestamp_seconds": source_time,
                "timebase": "recording",
            },
        },
    )
    assert response.status_code == 422


async def test_saved_reviewed_possession_events_reach_worker_producer(workspace, worker_python):  # noqa: F811
    client, db, session = workspace
    _, (clip,) = await film(db, session, [(0, 1)])
    tracks = []
    for side in ("offense", "defense"):
        response = await client.post(
            "/api/v1/tracklets",
            json={
                "clip_id": str(clip.id),
                "start_frame": 0,
                "end_frame": 10,
                "team_label": side,
                "provenance": {"source": "manual"},
                "track_points": [
                    {
                        "frame_number": i,
                        "timestamp_seconds": i / 10,
                        "shot_id": "0",
                        "visibility_state": "observed",
                        "bbox": [i, 0, i + 2, 4],
                        "field_x": 50 - i / 10,
                        "field_y": 2,
                    }
                    for i in range(11)
                ],
            },
        )
        assert response.status_code == 201, response.text
        tracks.append((await client.get(f"/api/v1/tracklets/{response.json()['id']}")).json())
    receiver, defender = (track["id"] for track in tracks)
    specifications = [
        (
            "pass_target",
            0.1,
            {
                "actor_id": receiver,
                "opponent_id": defender,
                "forward_pass": True,
                "responsibility_confirmed": True,
            },
        ),
        ("pass_result", 0.4, {"actor_id": receiver, "outcome": "complete", "contested": True}),
        (
            "reception",
            0.4,
            {
                "actor_id": receiver,
                "forward_pass": True,
                "completion_observed": True,
                "possession_side": "offense",
            },
        ),
        (
            "possession_end",
            1.0,
            {"actor_id": receiver, "possession_ended": True, "terminal_reason": "out_of_bounds"},
        ),
        ("carrier_cut", 0.6, {"actor_id": receiver, "cut_observed": True}),
        (
            "tackle_outcome",
            0.8,
            {
                "actor_id": defender,
                "opponent_id": receiver,
                "outcome": "missed",
                "tackle_observed": True,
                "forced_by_carrier": True,
            },
        ),
    ]
    for kind, time, attrs in specifications:
        saved = await client.post(
            "/api/v1/corrections",
            json={
                "clip_id": str(clip.id),
                "correction_type": "event_tag",
                "corrected_value": {
                    "event_type": kind,
                    "timestamp_seconds": time,
                    "timebase": "clip",
                    "attributes": {
                        "shot_id": "0",
                        "possession_id": "explicit-protocol-possession",
                        **attrs,
                    },
                },
            },
        )
        assert saved.status_code == 201, saved.text
    events = (
        await client.get(
            f"/api/v1/clips/{clip.id}/events", params={"human_only": True, "limit": 500}
        )
    ).json()
    assert len(events) == 6
    repo = Path(__file__).resolve().parents[2]
    code = """
import json,sys
from pipeline.observed_skill_outcomes import observed_skill_outcomes
d=json.load(sys.stdin)
print(json.dumps(observed_skill_outcomes(
    d['tracks'],d['events'],analytics_safe=True,attacking_direction=1)))
"""
    result = subprocess.run(  # noqa: S603 -- fixed worker producer; JSON enters stdin only
        [worker_python, "-c", code],
        cwd=repo / "gpu-worker",
        input=json.dumps({"tracks": tracks, "events": events}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    rows = {row["metric_name"]: row for row in json.loads(result.stdout.splitlines()[-1])}
    assert rows["observed_yac"]["metric_value"]["yards"] < 0
    assert rows["coverage_target_outcome"]["tracklet_id"] == defender
    assert rows["observed_missed_tackle"]["tracklet_id"] == defender
    assert rows["observed_forced_missed_tackle"]["tracklet_id"] == receiver
    assert rows["observed_cut_location"]["metric_value"]["actor_id"] == receiver
    assert all(row["experimental_flag"] for row in rows.values())
