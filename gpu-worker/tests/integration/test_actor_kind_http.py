"""Actual HTTP correction -> worker replay -> fenced publication protocol.

Only disposable synthetic film and manually submitted protocol observations are
used. Segment/track candidate generation and analytic consumers are test seams;
snapshot reads, orchestration replay, storage/PTS, leases and publication are real.
No football model performance, automatic identity or benchmark approval is claimed.
"""

import copy
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from scratch_database import require_scratch_database
from test_generic_label_producer_http import _prioritize
from test_pipeline_roundtrip import (
    _ADMIN,
    _ROOT,
    _WORKER,
    _backend_python,
    _poll,
    _synthetic_video,
    backend,  # noqa: F401 -- real disposable loopback API fixture
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def scratch_only():
    database = os.environ.get("DATABASE_URL")
    if not database:
        pytest.skip("DATABASE_URL not configured")
    require_scratch_database(database)


def _points():
    return [
        {
            "frame_number": i,
            "timestamp_seconds": i / 30,
            "shot_id": "protocol-shot",
            "visibility_state": "observed",
            "bbox": [i + 5, 10, i + 15, 30],
        }
        for i in range(11)
    ]


def _damage_binding(label_id, mode):
    """Corrupt ONLY this fixture's own label, simulating legacy/stale evidence."""
    script = """
import asyncio,json,os,uuid
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Label
async def main():
 async with AsyncSessionLocal() as db:
  label=(await db.execute(select(Label).where(Label.id==uuid.UUID(os.environ['ACTOR_LABEL_ID'])))).scalar_one()
  value=dict(label.label_value)
  if os.environ['ACTOR_DAMAGE']=='missing':value.pop('actor_bindings',None)
  else:
   binding=json.loads(json.dumps(value['actor_bindings']))
   binding['scope']['media_sha256']='f'*64
   value['actor_bindings']=binding
  label.label_value=value
  await db.commit()
asyncio.run(main())
"""
    result = subprocess.run(
        [_backend_python(), "-c", script],
        cwd=_ROOT.parent / "backend",
        env={
            **os.environ,
            "SECRET_KEY": "integration-secret",
            "ACTOR_LABEL_ID": label_id,
            "ACTOR_DAMAGE": mode,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_staff_kind_survives_two_generations_and_next_correction(backend, tmp_path, monkeypatch):  # noqa: F811
    from pipeline import backend as worker_api
    from pipeline import orchestrator
    from pipeline.runtime import job_context

    started = time.monotonic()
    trace = []
    worker_id = f"actor-kind-protocol-{uuid.uuid4().hex}"
    with httpx.Client(base_url=backend, timeout=120) as client:

        def login(credentials):
            response = client.post("/api/v1/auth/login", json=credentials)
            response.raise_for_status()
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        staff, worker = login(_ADMIN), login(_WORKER)
        response = client.post(
            "/api/v1/system/workers/register",
            headers=worker,
            json={
                "worker_id": worker_id,
                "device": "cpu",
                "profiles": ["review"],
                "job_types": ["pipeline"],
            },
        )
        response.raise_for_status()
        source = tmp_path / "synthetic-actor-protocol.mp4"
        _synthetic_video(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        uploaded = client.post(
            "/api/v1/videos/upload-url", headers=staff, json={"filename": source.name}
        )
        uploaded.raise_for_status()
        put = client.put(
            uploaded.json()["uploadUrl"],
            headers={**staff, "Content-Type": "video/mp4"},
            content=source.read_bytes(),
        )
        put.raise_for_status()
        registered = client.post(
            "/api/v1/videos",
            headers=staff,
            json={
                "filename": source.name,
                "storage_uri": put.json()["storageUri"],
                "session_kind": "practice",
            },
        )
        registered.raise_for_status()
        video_id = registered.json()["id"]
        video = _poll(
            lambda: client.get(f"/api/v1/videos/{video_id}", headers=staff).json(),
            lambda value: value.get("content_sha256") == digest,
        )
        initial_jobs = _poll(
            lambda: client.get(f"/api/v1/videos/{video_id}/jobs", headers=staff).json(),
            lambda values: bool(values),
        )
        for job in initial_jobs:
            client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=staff).raise_for_status()
        clip_response = client.post(
            f"/api/v1/videos/{video_id}/clips",
            headers=staff,
            json={
                "start_time": 0,
                "end_time": 1,
                "boundary_source": "human",
                "provenance": {"source": "human", "protocol_fixture": True},
            },
        )
        clip_response.raise_for_status()
        clip = clip_response.json()
        clip_id = clip["id"]
        _poll(
            lambda: client.get(f"/api/v1/clips/{clip_id}", headers=staff),
            lambda response: response.status_code == 200,
        )
        original = client.post(
            "/api/v1/tracklets",
            headers=staff,
            json={
                "clip_id": clip_id,
                "start_frame": 0,
                "end_frame": 10,
                "team_label": "official",
                "track_points": _points(),
                "provenance": {
                    "source": "human",
                    "actor_kind": "official",
                    "protocol_fixture": True,
                },
            },
        )
        original.raise_for_status()
        current = original.json()
        expected_player_id = None

        def labels(track_id):
            response = client.get(f"/api/v1/tracklets/{track_id}/labels", headers=staff)
            response.raise_for_status()
            return response.json()

        def correction(value):
            response = client.post(
                "/api/v1/corrections",
                headers=staff,
                json={
                    "clip_id": clip_id,
                    "correction_type": "player_identity",
                    "corrected_value": {"tracklet_id": current["id"], **value},
                },
            )
            response.raise_for_status()
            return _poll(
                lambda: labels(current["id"]),
                lambda values: any(
                    label["source"] == "human"
                    and all(label["label_value"].get(k) == v for k, v in value.items())
                    for label in values
                ),
            )[-1]

        first = correction({"actor_kind": "player"})
        assert first["label_value"]["actor_bindings"]["scope"]["media_sha256"] == digest
        assert set(first["label_value"]["actor_bindings"]["references"]) == {current["id"]}

        def rerun(expected_kind, *, hold=False):
            nonlocal current
            response = client.post(
                "/api/v1/jobs",
                headers=staff,
                json={
                    "video_id": video_id,
                    "job_type": "pipeline",
                    "processing_profile": "review",
                    "priority": 10,
                    "input_artifacts": {"auto_detailed_analysis": False, "protocol_fixture": True},
                },
            )
            response.raise_for_status()
            job_id = response.json()["id"]
            _poll(
                lambda: client.get(f"/api/v1/jobs/{job_id}", headers=staff).json(),
                lambda row: row.get("status") == "queued",
            )
            _prioritize(job_id)
            claim = client.post(
                "/api/v1/jobs/claim",
                headers=worker,
                json={"worker_id": worker_id, "lease_seconds": 120, "job_types": ["pipeline"]},
            )
            claim.raise_for_status()
            leased = claim.json()
            assert leased["id"] == job_id
            fence = {
                "job_id": job_id,
                "worker_id": worker_id,
                "lease_token": leased["lease_token"],
                "attempt_count": leased["attempt_count"],
            }
            consumers = []
            fresh = []

            def video_stage(stage, ctx):
                assert stage == "segment"
                return {
                    "clips": [copy.deepcopy(clip)],
                    "data_source": "manual",
                    "protocol_fixture": True,
                }

            def clip_stage(stage, ctx, window):
                if stage == "track":
                    # Normal runs reproduce the false official candidate. Negative
                    # runs start with an automatic player, which must also be held
                    # outside player consumers when staff evidence cannot bind.
                    candidate_kind = "player" if hold else "official"
                    record = worker_api.create_tracklet(
                        clip_id,
                        0,
                        10,
                        _points(),
                        team_label="home" if hold else "official",
                        job_id=job_id,
                        provenance={
                            "source": "human",
                            "actor_kind": candidate_kind,
                            "protocol_fixture": True,
                        },
                    )
                    record["class"] = candidate_kind
                    fresh.append(record)
                    return {
                        "tracklets": [record] if hold else [],
                        "non_player_tracklets": [] if hold else [record],
                        "data_source": "manual",
                    }
                state = ctx.clip_artifacts[clip_id]["track"]
                consumers.append(
                    {
                        "stage": stage,
                        "players": copy.deepcopy(state["tracklets"]),
                        "other": copy.deepcopy(state["non_player_tracklets"]),
                    }
                )
                if expected_kind == "player" and not hold:
                    assert len(state["tracklets"]) == 1 and not state["non_player_tracklets"]
                    assert state["tracklets"][0]["class"] == "player"
                    assert state["tracklets"][0].get("player_id") == expected_player_id
                else:
                    assert state["tracklets"] == [], (
                        "Non-player/unresolved actor leaked into player analysis"
                    )
                return {}

            with job_context(lease=fence), monkeypatch.context() as patch:
                snapshot = worker_api.fetch_annotation_snapshot(video_id)
                effective = next(row for row in snapshot if row["id"] == clip_id)
                assert effective["actor_kind_labels"]
                assert all(
                    label["tracklet_id"] == current["id"]
                    for label in effective["actor_kind_labels"]
                )
                patch.setattr(orchestrator, "_run_video_stage", video_stage)
                patch.setattr(orchestrator, "_run_clip_stage", clip_stage)
                result = orchestrator.run_pipeline(
                    video_id,
                    video["storage_uri"],
                    job_id=job_id,
                    stages=["segment", "track", "reid", "metrics"],
                    processing_profile="review",
                    revisions={"effective_annotations": snapshot},
                    lineage={"source_registration": video, "existing_clips": [clip]},
                )
            assert result["failed_clip_stages"] == [] and result["failed_video_stages"] == []
            assert result["source_identity"]["full_sha256"] == digest
            assert [row["stage"] for row in consumers] == ["reid", "metrics"]
            issues = result["clips"][0]["actor_kind_review_required"]
            assert bool(issues) is hold
            completed = client.patch(
                f"/api/v1/jobs/{job_id}",
                headers=worker,
                json={
                    "status": "succeeded",
                    **{k: v for k, v in fence.items() if k != "job_id"},
                    "output_artifacts": result,
                },
            )
            completed.raise_for_status()
            job = _poll(
                lambda: client.get(f"/api/v1/jobs/{job_id}", headers=staff).json(),
                lambda value: value.get("status") == "succeeded",
            )
            published = client.get(f"/api/v1/clips/{clip_id}/tracklets", headers=staff).json()
            if hold:
                assert [row["id"] for row in published] == [current["id"]]
                assert (
                    job["output_artifacts"]["publication_holds"][clip_id]
                    == "actor_kind_rebinding_unresolved"
                )
                overlay = client.get(f"/api/v1/clips/{clip_id}/overlays", headers=staff)
                overlay.raise_for_status()
                assert any(
                    "person-kind correction could not be matched" in warning
                    for warning in overlay.json()["warnings"]
                )
            else:
                published = _poll(
                    lambda: client.get(f"/api/v1/clips/{clip_id}/tracklets", headers=staff).json(),
                    lambda values: len(values) == 1 and values[0]["id"] == fresh[0]["id"],
                )
                current = published[0]
                assert current["provenance"]["actor_kind"] == expected_kind
                inherited = next(
                    label for label in labels(current["id"]) if label["source"] == "inherited_human"
                )
                assert inherited["annotated_by"]
                assert inherited["label_value"]["actor_kind"] == expected_kind
                assert inherited["label_value"]["actor_review_origin_label_id"]
            trace.append(
                {
                    "job_id": job_id,
                    "kind": expected_kind,
                    "held": hold,
                    "player_consumer_counts": [len(row["players"]) for row in consumers],
                    "snapshot_sources": [
                        label["source"] for label in effective["actor_kind_labels"]
                    ],
                }
            )
            return job

        rerun("player")
        inherited = next(
            label for label in labels(current["id"]) if label["source"] == "inherited_human"
        )
        assert inherited["label_value"]["actor_review_origin_label_id"] == first["id"]
        roster = client.post(
            "/api/v1/players",
            headers=staff,
            json={
                "first_name": "Protocol",
                "last_name": f"Actor-{uuid.uuid4().hex}",
                "jersey_number": 42,
            },
        )
        roster.raise_for_status()
        expected_player_id = roster.json()["id"]
        correction({"player_id": expected_player_id})
        second = correction({"jersey_number": 42, "team_label": "defense"})
        assert second["label_value"]["actor_kind"] == "player"
        assert second["label_value"]["player_id"] == expected_player_id
        assert set(second["label_value"]["actor_bindings"]["references"]) == {current["id"]}
        rerun("player")
        assert current["team_label"] == "defense"
        assert current["player_id"] == expected_player_id
        inherited = next(
            label for label in labels(current["id"]) if label["source"] == "inherited_human"
        )
        assert inherited["label_value"]["jersey_number"] == 42
        assert inherited["label_value"]["actor_review_origin_label_id"] == second["id"]
        rerun("player")  # Inherited-only current snapshot must retain original human authority.
        inherited = next(
            label for label in labels(current["id"]) if label["source"] == "inherited_human"
        )
        assert inherited["label_value"]["jersey_number"] == 42
        assert inherited["label_value"]["actor_review_origin_label_id"] == second["id"]
        third = correction({"actor_kind": "official"})
        rerun("official")
        assert current["player_id"] is None and current["team_label"] == "official"
        inherited = next(
            label for label in labels(current["id"]) if label["source"] == "inherited_human"
        )
        assert inherited["label_value"]["actor_review_origin_label_id"] == third["id"]
        _damage_binding(inherited["id"], "source_changed")
        rerun("unknown", hold=True)
        _damage_binding(inherited["id"], "missing")
        rerun("unknown", hold=True)
        report = {
            "schema_version": 1,
            "status": "passed_protocol_only",
            "football_validation": False,
            "video_id": video_id,
            "source_sha256": digest,
            "runs": trace,
            "duration_seconds": round(time.monotonic() - started, 2),
        }
        evidence = os.environ.get("ACTOR_KIND_HTTP_EVIDENCE")
        if evidence:
            destination = Path(evidence)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2) + "\n")
