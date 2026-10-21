"""Disposable PostgreSQL staff-save -> fresh generation -> producer contracts."""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.models import (
    CoachCorrection,
    Event,
    JobStatus,
    Label,
    Player,
    ProcessingJob,
    Tracklet,
    Video,
    WorkerRuntime,
)
from app.services.actor_binding_protocol import observation_fingerprint
from app.services.actor_bindings import verified_manual_identity
from sqlalchemy import select
from test_clip_edits import film, workspace  # noqa: F401 -- shared rollback fixture

REPO = Path(__file__).resolve().parents[2]


def points():
    return [
        {
            "frame_number": i,
            "timestamp_seconds": i / 10,
            "shot_id": 0,
            "visibility_state": "observed",
            "bbox": [i, 0, i + 2, 4],
            "field_x": 50 + i / 10,
            "field_y": 2,
        }
        for i in range(11)
    ]


async def create_track(client, clip, **extras):
    response = await client.post(
        "/api/v1/tracklets",
        json={
            "clip_id": str(clip.id),
            "start_frame": 0,
            "end_frame": 10,
            "track_points": points(),
            "provenance": {"source": "synthetic_test"},
            **extras,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def correction(client, clip, attrs):
    result = await client.post(
        "/api/v1/corrections",
        json={
            "clip_id": str(clip.id),
            "correction_type": "event_tag",
            "corrected_value": {
                "event_type": "position_participation",
                "timestamp_seconds": 0,
                "attributes": {
                    "shot_id": "0",
                    "observed_role": "WR",
                    "team_label": "offense",
                    "opportunity_kind": "routes",
                    "end_seconds": 1,
                    **attrs,
                },
            },
        },
    )
    assert result.status_code == 201, result.text
    event_id = result.json()["corrected_value"]["event_id"]
    return (await client.get(f"/api/v1/events/{event_id}")).json()


@pytest.mark.parametrize("manual", [False, True])
async def test_actual_staff_save_new_job_ids_and_worker_producer(
    workspace,  # noqa: F811 -- imported shared fixture
    monkeypatch,
    tmp_path,
    manual,
    worker_python,
):
    client, db, session = workspace
    video, (clip,) = await film(db, session, [(0, 1)])
    video.content_sha256 = uuid.uuid4().hex * 2
    video.size_bytes = 100
    # Explicit verified source fixture. No football accuracy or model approval.
    monkeypatch.setattr("app.services.media_identity.verified_media_sha256", _fixture_media)
    old = await create_track(client, clip)
    player = Player(first_name="Synthetic", last_name="Contract")
    db.add(player)
    await db.flush()
    if manual:
        identity = await client.post(
            "/api/v1/corrections",
            json={
                "clip_id": str(clip.id),
                "correction_type": "player_identity",
                "corrected_value": {"tracklet_id": old["id"], "player_id": str(player.id)},
            },
        )
        assert identity.status_code == 201, identity.text
    else:
        # A player_id written by an algorithm is not manual identity lineage.
        original = (
            await db.execute(select(Tracklet).where(Tracklet.id == uuid.UUID(old["id"])))
        ).scalar_one()
        original.player_id = player.id
        db.add(
            Label(
                clip_id=clip.id,
                tracklet_id=original.id,
                label_type="player_identity",
                label_value={"player_id": str(player.id)},
                source="model",
            )
        )
    saved = await correction(
        client,
        clip,
        {
            "actor_id": old["id"],
            "actor_bindings": {"forged": True},
            "actor_binding_lineage": {"forged": "player"},
        },
    )
    assert "actor_bindings" not in saved["attributes"]
    binding = saved["provenance"]["actor_bindings"]
    assert binding["protocol"] == "native-observations-v1"
    assert binding["scope"]["registration_fingerprint"] == video.content_sha256
    assert binding["references"][old["id"]]["observation_sha256"] == observation_fingerprint(old)
    assert bool(binding["references"][old["id"]].get("manual_identity")) is manual
    new_job = ProcessingJob(video_id=video.id, job_type="pipeline")
    db.add(new_job)
    await db.flush()
    fresh = await create_track(client, clip, job_id=str(new_job.id))
    assert fresh["id"] != old["id"] and fresh["job_id"] == str(new_job.id)
    payload = tmp_path / "rebind.json"
    payload.write_text(
        json.dumps({"events": [saved], "tracks": [fresh], "scope": binding["scope"]}),
        encoding="utf-8",
    )
    # Use the actual worker environment, not backend imports of CV dependencies.
    script = """
import json,sys
from pipeline.actor_bindings import rebind_reviewed_events
from pipeline.observed_position_products import hydrate_position_roles, observed_position_products
data=json.load(open(sys.argv[1], encoding='utf-8'))
events,tracks,issues=rebind_reviewed_events(data['events'],data['tracks'],data['scope'])
tracks=hydrate_position_roles(tracks,events)
rows=observed_position_products(tracks,events)
print(json.dumps({'events':events,'tracks':tracks,'issues':issues,'rows':rows}))
"""
    result = subprocess.run(  # noqa: S603 -- fixed worker interpreter/script, JSON fixture argument
        [worker_python, "-c", script, str(payload)],
        cwd=REPO / "gpu-worker",
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(result.stdout)
    assert output["issues"] == []
    assert output["events"][0]["id"] == saved["id"]
    assert output["events"][0]["attributes"]["actor_id"] == fresh["id"]
    opportunities = [r for r in output["rows"] if r["metric_name"] == "position_opportunity"]
    assert bool(opportunities) is manual
    if manual:
        assert output["tracks"][0]["player_id"] == str(player.id)
        assert opportunities[0]["metric_value"]["actor_id"] == fresh["id"]
        track = (
            await db.execute(select(Tracklet).where(Tracklet.id == uuid.UUID(fresh["id"])))
        ).scalar_one()
        proof = await verified_manual_identity(
            db, clip.id, track, output["tracks"][0]["provenance"]["manual_identity_binding"]
        )
        assert proof and proof["player_id"] == str(player.id)
        new_job.status = JobStatus.running
        new_job.lease_token = uuid.uuid4()
        new_job.leased_by = "binding-fixture-worker"
        new_job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
        await db.flush()
        headers = {
            "X-Job-ID": str(new_job.id),
            "X-Job-Lease-Token": str(new_job.lease_token),
            "X-Worker-ID": new_job.leased_by,
        }
        persisted = await client.patch(
            f"/api/v1/tracklets/{fresh['id']}",
            headers=headers,
            json={"player_id": str(player.id), "provenance": output["tracks"][0]["provenance"]},
        )
        assert persisted.status_code == 200, persisted.text
        assert persisted.json()["player_id"] == str(player.id)
        assert (
            persisted.json()["provenance"]["manual_identity_binding"]["label_id"]
            == proof["label_id"]
        )
        # A changed source observation cannot gain manual identity via a label ID.
        changed_points = points()
        changed_points[-1]["bbox"][0] += 1
        changed = await create_track(
            client, clip, job_id=str(new_job.id), track_points=changed_points
        )
        denied = await client.patch(
            f"/api/v1/tracklets/{changed['id']}",
            headers=headers,
            json={"player_id": str(player.id), "provenance": output["tracks"][0]["provenance"]},
        )
        assert denied.status_code == 422
        unassign = await client.post(
            "/api/v1/corrections",
            json={
                "clip_id": str(clip.id),
                "correction_type": "player_identity",
                "corrected_value": {"tracklet_id": fresh["id"], "player_id": None},
            },
        )
        # Pending writeback is unavailable for staff editing until publication.
        assert unassign.status_code == 409
        track.provenance = {**track.provenance, "publication_state": "published"}
        await db.flush()
        unassign = await client.post(
            "/api/v1/corrections",
            json={
                "clip_id": str(clip.id),
                "correction_type": "player_identity",
                "corrected_value": {"tracklet_id": fresh["id"], "player_id": None},
            },
        )
        assert unassign.status_code == 201, unassign.text
        assert (
            await verified_manual_identity(
                db, clip.id, track, output["tracks"][0]["provenance"]["manual_identity_binding"]
            )
            is None
        )
    else:
        assert output["tracks"][0].get("player_id") is None
    assert (await client.get(f"/api/v1/events/{saved['id']}")).json() == saved


async def _fixture_media(db, video: Video):
    return "b" * 64


async def test_foreign_and_unmeasured_actor_capture_requires_review(workspace, monkeypatch):  # noqa: F811
    client, db, session = workspace
    video, clips = await film(db, session, [(0, 1), (1, 2)])
    video.content_sha256 = uuid.uuid4().hex * 2
    monkeypatch.setattr("app.services.media_identity.verified_media_sha256", _fixture_media)
    foreign = await create_track(client, clips[1])
    event = await correction(client, clips[0], {"actor_id": foreign["id"]})
    binding = event["provenance"]["actor_bindings"]
    assert not binding["references"]
    assert binding["review_required"][0]["reason"] == "actor_reference_not_active_in_clip"


async def test_missing_source_proof_is_not_supplied_by_client_metadata(workspace):  # noqa: F811
    client, db, session = workspace
    _, (clip,) = await film(db, session, [(0, 1)])
    old = await create_track(client, clip)
    saved = await correction(
        client,
        clip,
        {"actor_id": old["id"], "actor_bindings": {"scope": {"media_sha256": "f" * 64}}},
    )
    binding = saved["provenance"]["actor_bindings"]
    assert binding["scope"]["media_sha256"] is None
    assert binding["review_required"][0]["reason"] == "actor_binding_source_identity_unverified"


async def test_direct_manual_event_captures_bindings_and_rejects_bad_actor_container(workspace):  # noqa: F811
    client, db, session = workspace
    video, (clip,) = await film(db, session, [(0, 1)])
    video.content_sha256, video.size_bytes = uuid.uuid4().hex * 2, 10
    old = await create_track(client, clip)
    response = await client.post(
        "/api/v1/events",
        json={
            "clip_id": str(clip.id),
            "event_type": "snap",
            "timestamp_seconds": 0,
            "provenance": {"source": "manual", "actor_bindings": {"forged": True}},
            "attributes": {"actor_id": old["id"], "timebase": "clip", "ol_roles": ["bad"]},
        },
    )
    assert response.status_code == 201, response.text
    binding = response.json()["provenance"]["actor_bindings"]
    assert binding["protocol"] == "native-observations-v1" and "forged" not in binding
    assert binding["review_required"][0] == {
        "reason": "actor_reference_container_invalid",
        "fields": ["ol_roles"],
    }
    assert (await client.get(f"/api/v1/clips/{clip.id}")).json()["annotation_revision"] == 1


def test_protocol_is_identical_across_independent_service_packages():
    assert (REPO / "backend/app/services/actor_binding_protocol.py").read_text() == (
        REPO / "gpu-worker/pipeline/actor_binding_protocol.py"
    ).read_text()


async def test_persisted_replay_review_is_fenced_and_event_patch_clears_obsolete_warning(
    workspace,  # noqa: F811 -- imported shared fixture
    monkeypatch,
):
    client, db, session = workspace
    video, (clip,) = await film(db, session, [(0, 1)])
    video.content_sha256, video.size_bytes = uuid.uuid4().hex * 2, 10
    monkeypatch.setattr("app.services.media_identity.verified_media_sha256", _fixture_media)
    old = await create_track(client, clip)
    saved = await correction(client, clip, {"actor_id": old["id"]})
    revision = (await client.get(f"/api/v1/clips/{clip.id}")).json()["annotation_revision"]
    job = ProcessingJob(
        video_id=video.id,
        job_type="pipeline",
        status=JobStatus.running,
        lease_token=uuid.uuid4(),
        leased_by=f"review-{uuid.uuid4()}",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    db.add(job)
    db.add(WorkerRuntime(worker_id=job.leased_by, registered_by=uuid.UUID(saved["created_by"])))
    await db.flush()
    headers = {
        "X-Job-ID": str(job.id),
        "X-Job-Lease-Token": str(job.lease_token),
        "X-Worker-ID": job.leased_by,
    }
    body = {
        "job_id": str(job.id),
        "annotation_revision": revision,
        "issues": [
            {
                "event_id": saved["id"],
                "actor_ids": [old["id"]],
                "reason": "actor_binding_observations_missing_or_changed",
                "review_required": True,
            }
        ],
    }
    path = f"/api/v1/clips/{clip.id}/actor-binding-review"
    assert (await client.post(path, json=body)).status_code == 403
    assert (
        await client.post(
            path, headers={**headers, "X-Job-Lease-Token": str(uuid.uuid4())}, json=body
        )
    ).status_code == 409
    assert (
        await client.post(path, headers=headers, json={**body, "annotation_revision": revision + 1})
    ).status_code == 409
    bad = {**body, "issues": [{**body["issues"][0], "reason": "<script>unsafe</script>"}]}
    assert (await client.post(path, headers=headers, json=bad)).status_code == 422
    assert (await client.post(path, headers=headers, json=body)).status_code == 200
    review = (await client.get(path)).json()
    assert review["observations"][0]["event"]["id"] == saved["id"]
    assert review["observations"][0]["issues"][0]["reason"] == body["issues"][0]["reason"]
    assert (await client.get(f"/api/v1/events/{saved['id']}")).json() == saved
    newer = ProcessingJob(
        video_id=video.id, job_type="pipeline", created_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    db.add(newer)
    await db.flush()
    assert not (await client.get(path)).json()["observations"]
    assert (await client.post(path, headers=headers, json=body)).status_code == 409
    await db.delete(newer)
    await db.flush()
    # Current staff edits the SAME saved event, rather than adding another one.
    update = await client.patch(
        f"/api/v1/events/{saved['id']}",
        json={"attributes": {**saved["attributes"], "shot_id": "0"}, "expected_revision": revision},
    )
    assert update.status_code == 200, update.text
    assert update.json()["id"] == saved["id"]
    assert not (await client.get(path)).json()["observations"]
    assert (
        len((await db.execute(select(Event).where(Event.clip_id == clip.id))).scalars().all()) == 1
    )
    assert (
        await client.patch(
            f"/api/v1/events/{saved['id']}",
            json={"attributes": saved["attributes"], "expected_revision": revision},
        )
    ).status_code == 409
    assert (
        await client.delete(f"/api/v1/events/{saved['id']}?expected_revision={revision}")
    ).status_code == 409
    _, (foreign,) = await film(db, session, [(0, 1)])
    assert (
        await client.post(
            f"/api/v1/clips/{foreign.id}/actor-binding-review", headers=headers, json=body
        )
    ).status_code == 409
    removed = await client.delete(f"/api/v1/events/{saved['id']}?expected_revision={revision + 1}")
    assert removed.status_code == 204
    preserved = (await client.get(f"/api/v1/events/{saved['id']}")).json()
    assert preserved["provenance"]["state"] == "superseded"
    assert preserved["attributes"] == update.json()["attributes"]
    assert (
        await client.patch(f"/api/v1/events/{saved['id']}", json={"timestamp_seconds": 0.1})
    ).status_code == 409
    history = (
        (await db.execute(select(CoachCorrection).where(CoachCorrection.clip_id == clip.id)))
        .scalars()
        .all()
    )
    removal = next(row for row in history if row.corrected_value.get("removed") is True)
    assert removal.original_value["event"]["id"] == saved["id"]
    assert removal.original_value["event"]["attributes"] == update.json()["attributes"]
    assert removal.training_eligible is False
    revision = (await client.get(f"/api/v1/clips/{clip.id}")).json()["annotation_revision"]
    removal_review = await client.post(
        path,
        headers=headers,
        json={
            "job_id": str(job.id),
            "annotation_revision": revision,
            "issues": [],
            "removal_issues": [
                {"event_id": saved["id"], "reason": "removed_event_identity_unresolved"}
            ],
        },
    )
    assert removal_review.status_code == 200, removal_review.text
    unresolved = (await client.get(path)).json()
    assert unresolved["observations"] == []
    assert unresolved["removed_observations"][0]["event"]["id"] == saved["id"]


@pytest.mark.parametrize(
    "types,family",
    [
        (("snap", "snap"), "snap"),
        (("pass_release", "throw"), "pass_release"),
        (("forward_pass_release", "pass_release"), "pass_release"),
        (("catch", "catch"), "catch"),
        (("handoff", "handoff"), "handoff"),
    ],
)
async def test_duplicate_human_prediction_clocks_need_review_and_soft_removal_clears_ambiguity(
    workspace,  # noqa: F811 -- imported shared fixture
    types,
    family,
    monkeypatch,
):
    client, db, session = workspace
    video, (clip,) = await film(db, session, [(0, 1)])
    video.content_sha256, video.size_bytes = uuid.uuid4().hex * 2, 10
    monkeypatch.setattr("app.services.media_identity.verified_media_sha256", _fixture_media)
    actors = [await create_track(client, clip), await create_track(client, clip)]
    ids = []
    for index, kind in enumerate([*types, "pass_result", "pass_result"]):
        response = await client.post(
            "/api/v1/corrections",
            json={
                "clip_id": str(clip.id),
                "correction_type": "event_tag",
                "corrected_value": {
                    "event_type": kind,
                    "timestamp_seconds": index / 10,
                    "attributes": {"shot_id": "0", "actor_id": actors[index % 2]["id"]},
                },
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["corrected_value"]["event_id"])
    path = f"/api/v1/clips/{clip.id}/actor-binding-review"
    review = (await client.get(path)).json()
    ambiguous = [
        row
        for row in review["observations"]
        if any(i["reason"] == "prediction_clock_ambiguous" for i in row["issues"])
    ]
    assert {row["event"]["id"] for row in ambiguous} == set(ids[:2])
    assert all(
        next(i for i in row["issues"] if i["reason"] == "prediction_clock_ambiguous")[
            "clock_family"
        ]
        == family
        for row in ambiguous
    )
    revision = review["annotation_revision"]
    patched = await client.patch(
        f"/api/v1/events/{ids[0]}", json={"timestamp_seconds": 0.05, "expected_revision": revision}
    )
    assert patched.status_code == 200 and patched.json()["id"] == ids[0]
    assert len((await client.get(path)).json()["observations"]) == 2
    assert (
        await client.delete(f"/api/v1/events/{ids[1]}?expected_revision={revision}")
    ).status_code == 409
    removed = await client.delete(f"/api/v1/events/{ids[1]}?expected_revision={revision + 1}")
    assert removed.status_code == 204
    assert (await client.get(path)).json()["observations"] == []
    active = (await client.get(f"/api/v1/clips/{clip.id}/events?human_only=true")).json()
    assert {row["id"] for row in active} == {ids[0], ids[2], ids[3]}
    original = (await client.get(f"/api/v1/events/{ids[1]}")).json()
    assert original["provenance"]["state"] == "superseded"
    assert original["timestamp_seconds"] == 0.1
    removed_rows = (await client.get(f"/api/v1/clips/{clip.id}/removed-events")).json()
    assert [row["id"] for row in removed_rows] == [ids[1]]
    assert removed_rows[0]["provenance"]["removal"]["original_event"]["id"] == ids[1]
    forged = await client.post(
        "/api/v1/events",
        json={
            "clip_id": str(clip.id),
            "event_type": "snap",
            "timestamp_seconds": 0.1,
            "provenance": {
                "source": "synthetic_test",
                "state": "superseded",
                "removal": removed_rows[0]["provenance"]["removal"],
            },
        },
    )
    assert forged.status_code == 201, forged.text
    assert "removal" not in forged.json()["provenance"]
    assert len((await client.get(f"/api/v1/clips/{clip.id}/removed-events")).json()) == 1


async def test_actual_api_removal_reaches_new_worker_before_event_persistence(
    workspace,  # noqa: F811 -- imported shared fixture
    monkeypatch,
    tmp_path,
    worker_python,
):
    """Synthetic contract: saved removal survives exact new-generation UUIDs."""
    client, db, session = workspace
    video, (clip,) = await film(db, session, [(0, 1)])
    video.content_sha256, video.size_bytes = uuid.uuid4().hex * 2, 10
    monkeypatch.setattr("app.services.media_identity.verified_media_sha256", _fixture_media)
    old = await create_track(client, clip)
    response = await client.post(
        "/api/v1/events",
        json={
            "clip_id": str(clip.id),
            "event_type": "snap",
            "frame_number": 1,
            "timestamp_seconds": 0.1,
            "attributes": {"actor_id": old["id"], "shot_id": 0, "timebase": "recording"},
            "provenance": {"source": "synthetic_test_model"},
        },
    )
    assert response.status_code == 201, response.text
    saved = response.json()
    revision = (await client.get(f"/api/v1/clips/{clip.id}")).json()["annotation_revision"]
    assert (
        await client.delete(f"/api/v1/events/{saved['id']}?expected_revision={revision}")
    ).status_code == 204
    removed = (await client.get(f"/api/v1/clips/{clip.id}/removed-events")).json()
    assert [row["id"] for row in removed] == [saved["id"]]
    new_job = ProcessingJob(video_id=video.id, job_type="pipeline")
    db.add(new_job)
    await db.flush()
    fresh = await create_track(client, clip, job_id=str(new_job.id))
    assert fresh["id"] != old["id"]
    scope = removed[0]["provenance"]["removal"]["actor_bindings"]["scope"]
    payload = tmp_path / "removed.json"
    payload.write_text(
        json.dumps({"removed": removed, "fresh": fresh, "scope": scope}), encoding="utf-8"
    )
    script = """
import json,sys
from copy import deepcopy
from pipeline import orchestrator,stage_events
from pipeline.video_timeline import VideoTimeline
data=json.load(open(sys.argv[1], encoding='utf-8'))
scope=data['scope']; fresh=data['fresh']; writes=[]
proposal={'event_type':'snap','frame_number':1,'timestamp_seconds':.1,
          'attributes':{'actor_id':fresh['id']},'source':'synthetic_test_model'}
stage_events._detect_multisignal=lambda *a,**k: ([deepcopy(proposal)],{})
stage_events.backend.create_event=lambda *a,**k: writes.append(k) or {'id':'new-proposal'}
def run(track):
    ctx=orchestrator.PipelineContext(video_id=scope['video_id'],input_uri='unused',
                                   job_id='synthetic-new-job',priority=10)
    ctx.fps=10;ctx.timeline=VideoTimeline(tuple(i/10 for i in range(11)),1.1,10)
    ctx.detections={i:[{'bbox':[0,0,2,4],'shot_id':0}] for i in range(11)}
    ctx.lineage={'source_sha256':scope['media_sha256']}
    ctx.effective_annotations={scope['clip_id']:{**scope,'removed_events':data['removed']}}
    ctx.clip_artifacts={scope['clip_id']:{'track':{'tracklets':[track]}}}
    return orchestrator._run_clip_stage('events',ctx,
        {'id':scope['clip_id'],'start_time':0,'end_time':1})
exact=run(fresh);exact_writes=len(writes)
changed=deepcopy(fresh);changed['track_points'][1]['bbox'][0]+=.1
different=run(changed)
print(json.dumps({'exact':exact,'exact_writes':exact_writes,
                  'changed':different,'total_writes':len(writes)}))
"""
    replay = subprocess.run(  # noqa: S603 -- fixed worker interpreter and fixture script
        [worker_python, "-c", script, str(payload)],
        cwd=REPO / "gpu-worker",
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(replay.stdout.strip().splitlines()[-1])
    assert output["exact_writes"] == 0 and output["exact"]["event_count"] == 0
    assert output["exact"]["event_removal_review_required"] == []
    assert output["total_writes"] == 1 and output["changed"]["event_count"] == 1
    assert output["changed"]["event_removal_review_required"][0]["event_id"] == saved["id"]
    # Replay must not mutate the original staff history.
    assert (await client.get(f"/api/v1/events/{saved['id']}")).json() == removed[0]
