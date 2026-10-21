"""Actual normal worker producer -> stored artifact -> staff download -> blank CSV.

Only synthetic protocol observations are supplied; no real football evidence or
trained/approved model is claimed by this cross-service contract.
"""

import json
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import get_settings
from app.models import JobStatus, JobType, ProcessingJob, WorkerRuntime
from test_clip_edits import film, workspace  # noqa: F401 -- rollback fixture


async def test_normal_features_download_and_blank_target_handoff(
    workspace,  # noqa: F811 -- imported pytest fixture
    worker_python,
    tmp_path,
    monkeypatch,
):
    client, db, session = workspace
    video, (clip,) = await film(db, session, [(1, 3)])
    video.content_sha256, video.size_bytes = "a" * 64, 1024
    from app.deps import get_current_user
    from app.main import app

    actor = app.dependency_overrides[get_current_user]()
    monkeypatch.setenv("SEED_WORKER_EMAIL", actor.email)
    worker_id, token = f"export-{uuid.uuid4()}", uuid.uuid4()
    job = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.running,
        leased_by=worker_id,
        lease_token=token,
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(job)
    db.add(WorkerRuntime(worker_id=worker_id, registered_by=actor.id))
    await db.flush()
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    code = """
import json, os, runpy
from pipeline.stage_expected_outcomes import run
from pipeline.expected_outcomes.export import export_feature_snapshots
fixture=runpy.run_path('tests/test_expected_outcomes.py')
event,tracks=fixture['_observed_release']()
scope=json.loads(os.environ['EXPORT_SCOPE'])
packet=run(scope['clip_id'],scope['job_id'],10,tracklets=tracks,events=[event],analytics_safe=True,
 lineage={'session_id':scope['session_id'],'recording_id':scope['video_id'],'canonical_play_id':scope['clip_id'],'media_sha256':'a'*64},persist=False)
assert packet['feature_snapshots']['completion']
result=export_feature_snapshots({scope['clip_id']:{'expected_outcomes':packet}},video_id=scope['video_id'],job_id=scope['job_id'],source_uri=scope['source_uri'],media_sha256='a'*64,source_clock_verified=True,clip_revisions={scope['clip_id']:{'annotation_revision':0,'observation_revision':0}})
print(json.dumps(result))
"""
    env = {
        **os.environ,
        "BACKEND_API_URL": "",
        "EXPORT_SCOPE": json.dumps(
            {
                "clip_id": str(clip.id),
                "video_id": str(video.id),
                "session_id": str(session.id),
                "job_id": str(job.id),
                "source_uri": video.storage_uri,
            }
        ),
    }
    produced = subprocess.run(  # noqa: S603 -- fixed local worker command, fixture arguments
        [worker_python, "-c", code],
        cwd=root / "gpu-worker",
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    descriptor = json.loads(produced.stdout.splitlines()[-1])
    source = {
        "schema_version": 1,
        "video_id": str(video.id),
        "storage_uri": video.storage_uri,
        "size_bytes": video.size_bytes,
        "registration_fingerprint": video.content_sha256,
        "registration_algorithm": "full-sha256",
        "full_sha256": video.content_sha256,
    }
    completed = await client.patch(
        f"/api/v1/jobs/{job.id}",
        json={
            "status": "succeeded",
            "worker_id": worker_id,
            "lease_token": str(token),
            "attempt_count": 1,
            "output_artifacts": {"expected_feature_export": descriptor, "source_identity": source},
        },
    )
    assert completed.status_code == 200, completed.text
    response = await client.get(f"/api/v1/learning/expected-outcomes/features/{job.id}/download")
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["sha256"] == descriptor["sha256"] and info["row_count"] == 1
    downloaded = await client.get(info["download_url"])
    assert downloaded.status_code == 200
    assert (
        downloaded.headers["content-disposition"]
        == f'attachment; filename="expected-features-{job.id}.json"'
    )
    assert (await client.get(info["download_url"].replace("sig=", "sig=bad"))).status_code == 403
    # New diagnostic film links reuse the actual qualified normal producer export,
    # not a nearest clock or guessed canonical recording.
    from app.services.model_cases import current_case_films
    from app.services.model_diagnostics import heldout_cases

    packet = next(iter(json.loads(downloaded.content)["feature_snapshots"].values()))
    diagnostic = heldout_cases(
        [{**packet, "split": "holdout", "target": {"value": 0, "annotation_sha256": "b" * 64}}],
        [0.25],
        0.5,
    )["cases"][0]
    linked = await current_case_films(db, [diagnostic])
    assert linked[diagnostic["case_id"]]["clip_id"] == str(clip.id)
    assert linked[diagnostic["case_id"]]["focus_seconds"] == 1.0
    assert linked[diagnostic["case_id"]]["source_export_sha256"] == info["sha256"]
    clip.label_data = {"_annotation_revision": 1}
    await db.flush()
    assert await current_case_films(db, [diagnostic]) == {}
    clip.label_data = {}
    await db.flush()
    video.content_sha256 = "c" * 64
    await db.flush()
    assert await current_case_films(db, [diagnostic]) == {}
    video.content_sha256 = "a" * 64
    from app.models import Play, PlayView

    alternate = Play(id=uuid.uuid4(), session_id=session.id)
    db.add(alternate)
    await db.flush()
    view = PlayView(id=uuid.uuid4(), clip_id=clip.id, play_id=alternate.id)
    db.add(view)
    await db.flush()
    assert await current_case_films(db, [diagnostic]) == {}
    await db.delete(view)
    await db.flush()
    path = tmp_path / "downloaded-features.json"
    path.write_bytes(downloaded.content)
    csv = tmp_path / "staff-targets.csv"
    template = subprocess.run(  # noqa: S603 -- fixed local worker command, fixture arguments
        [
            worker_python,
            "-m",
            "training.expected_outcomes",
            "targets-template",
            "--snapshots",
            str(path),
            "--snapshots-sha256",
            info["sha256"],
            "--output",
            str(csv),
        ],
        cwd=root / "gpu-worker",
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert json.loads(template.stdout.splitlines()[-1])["targets_filled"] is False
    import csv as csv_module

    rows = list(csv_module.DictReader(csv.open(encoding="utf-8", newline="")))
    assert (
        len(rows) == 1 and rows[0]["value"] == rows[0]["reviewed"] == rows[0]["reviewer_id"] == ""
    )
    # Hosted object-store artifacts use the same bounded hash check and an
    # API attachment response, avoiding browser cross-origin filename fallback.
    import io
    from unittest.mock import Mock

    from app.services import expected_outcomes

    s3 = Mock()
    s3.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(downloaded.content)}
    monkeypatch.setattr(expected_outcomes, "get_s3_client", lambda: s3)
    job.output_artifacts = {
        **job.output_artifacts,
        "expected_feature_export": {
            **descriptor,
            "artifact_uri": descriptor["artifact_uri"].replace("local://", "s3://"),
        },
    }
    await db.flush()
    hosted = await client.get(info["download_url"])
    assert hosted.status_code == 200 and hosted.content == downloaded.content
    assert hosted.headers["content-disposition"] == downloaded.headers["content-disposition"]
    s3.get_object.assert_called_once()
    job.output_artifacts = {**job.output_artifacts, "expected_feature_export": descriptor}
    await db.flush()
    fake = ProcessingJob(
        id=uuid.uuid4(),
        video_id=video.id,
        job_type=JobType.pipeline,
        status=JobStatus.succeeded,
        attempt_count=1,
        output_artifacts={"expected_feature_export": descriptor, "source_identity": source},
    )
    db.add(fake)
    await db.flush()
    untrusted = await client.get(f"/api/v1/learning/expected-outcomes/features/{fake.id}/download")
    assert untrusted.status_code == 422 and "trusted fenced" in untrusted.text
    clip.label_data = {"_annotation_revision": 1}
    await db.flush()
    assert (await client.get(info["download_url"])).status_code == 422
    clip.label_data = {}
    clip.label_data = {"_observation_revision": 1}
    await db.flush()
    assert (await client.get(info["download_url"])).status_code == 422
    clip.label_data = {}
    clip.provenance = {"state": "superseded"}
    await db.flush()
    assert (await client.get(info["download_url"])).status_code == 422
    clip.provenance = {}
    job.output_artifacts = {
        **job.output_artifacts,
        "expected_feature_export": {**descriptor, "sha256": "0" * 64},
    }
    await db.flush()
    denied = await client.get(f"/api/v1/learning/expected-outcomes/features/{job.id}/download")
    assert denied.status_code == 422
