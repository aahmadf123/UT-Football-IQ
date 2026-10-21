"""Actual correction POST -> stored native clock -> worker feature contract.

Numeric positions are explicit synthetic protocol fixtures, never football labels.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from test_actor_bindings import create_track
from test_clip_edits import film, workspace  # noqa: F401 -- isolated rollback fixture

REPO = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((REPO / "tests_local/fixtures/prediction-context.json").read_text())


async def test_saved_prediction_context_reaches_actual_worker_extractor(
    workspace,  # noqa: F811 -- imported isolated fixture
    tmp_path,
    worker_python,
):
    client, db, session = workspace
    video, (clip,) = await film(db, session, [(10, 20)])
    video.content_sha256, video.size_bytes = uuid.uuid4().hex * 2, 100
    await db.flush()
    tracks, mapping = [], {}
    for name, (x, y) in FIXTURE["coordinates"].items():
        track = await create_track(
            client,
            clip,
            start_frame=372,
            end_frame=375,
            team_label="defense" if name in {"dl", "lb", "db"} else "offense",
            provenance={"source": "human"},
            track_points=[
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
        )
        mapping[name] = track["id"]
        tracks.append(track)

    from app.services.actor_binding_protocol import rewrite_actor_references

    saved = []
    for case in FIXTURE["cases"]:
        attrs = rewrite_actor_references(
            {**case["attributes"], "actor_id": case["actor_id"], "shot_id": "native"}, mapping
        )
        response = await client.post(
            "/api/v1/corrections",
            json={
                "clip_id": str(clip.id),
                "correction_type": "event_tag",
                "corrected_value": {
                    "event_type": case["event_type"],
                    "timestamp_seconds": 12.5,
                    "frame_number": 375,
                    "timebase": "recording",
                    "attributes": attrs,
                },
            },
        )
        assert response.status_code == 201, response.text
        event_id = response.json()["corrected_value"]["event_id"]
        event = (await client.get(f"/api/v1/events/{event_id}")).json()
        assert event["timestamp_seconds"] == 12.5 and event["frame_number"] == 375
        assert event["attributes"]["timebase"] == "recording"
        assert event["attributes"]["feature_context_available_at_seconds"] == 12.4
        assert event["provenance"]["actor_bindings"]["review_required"] == []
        saved.append(event)
    payload = tmp_path / "saved-context.json"
    payload.write_text(json.dumps({"events": saved, "tracks": tracks}), encoding="utf-8")
    script = """
import json, sys
from pathlib import Path
from pipeline.expected_outcomes.extract import extract_snapshot
payload=json.loads(Path(sys.argv[1]).read_text())
lineage={'session_id':'protocol-session','recording_id':'protocol-recording','canonical_play_id':'protocol-play','media_sha256':'a'*64}
result={task:extract_snapshot(
    task,payload['tracks'],payload['events'],lineage,
    analytics_safe=True,capture_regime='fixed_sideline'
) for task in ('completion','rushing_gain','yac','pressure')}
print(json.dumps(result))
"""
    result = subprocess.run(  # noqa: S603 -- fixed script and isolated test-owned input
        [worker_python, "-c", script, str(payload)],
        cwd=REPO / "gpu-worker",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout)
    assert set(outputs) == {"completion", "rushing_gain", "yac", "pressure"}
    assert all(row["observation_clock"]["timestamp_seconds"] == 12.5 for row in outputs.values())
    assert outputs["pressure"]["features"]["blitzers"]["value"] == 0
    assert outputs["completion"]["subject"]["tracklet_id"] == mapping["qb"]
    assert outputs["rushing_gain"]["subject"]["tracklet_id"] == mapping["receiver"]
