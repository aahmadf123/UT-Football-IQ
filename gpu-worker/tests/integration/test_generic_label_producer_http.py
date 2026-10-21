"""Actual local CPU candidate -> authenticated labels -> heldout promotion protocol.

The numerical graphs/media are explicit scratch protocol fixtures, never real
football evidence or a football accuracy claim. Only the isolated test DB is used.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
from scratch_database import require_scratch_database
from test_pipeline_roundtrip import (
    _ADMIN,
    _ROOT,
    _WORKER,
    _backend_python,
    _load_worker_main,
    _poll,
    backend,  # noqa: F401 -- actual loopback server fixture
)

pytestmark = pytest.mark.integration


def _prioritize(job_id):
    # Select only this fixture's new job ahead of old scratch jobs, without
    # executing or modifying any unrelated job.
    script = """
import asyncio,os,uuid
from sqlalchemy import update
from app.database import AsyncSessionLocal
from app.models import ProcessingJob
async def main():
 async with AsyncSessionLocal() as db:
  await db.execute(update(ProcessingJob).where(ProcessingJob.id==uuid.UUID(os.environ['PROTOCOL_JOB_ID'])).values(priority=2147483647))
  await db.commit()
asyncio.run(main())
"""
    result = subprocess.run(
        [_backend_python(), "-c", script],
        cwd=_ROOT.parent / "backend",
        env={**os.environ, "SECRET_KEY": "integration-secret", "PROTOCOL_JOB_ID": job_id},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def seed(worker_id):
    require_scratch_database(os.environ["DATABASE_URL"])
    script = r"""
import asyncio,hashlib,json,os,uuid
from datetime import datetime,timedelta,UTC
from pathlib import Path
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import User,FootballSession,Video,Clip,Label,ProcessingJob,JobType,JobStatus,FieldCalibration
from app.storage import local_object_path
async def main():
 async with AsyncSessionLocal() as db:
  owner=(await db.execute(select(User).where(User.email==os.environ['PROTOCOL_ADMIN_EMAIL']))).scalar_one()
  result={'sessions':[],'sources':[]}
  for split,count in [('train',30),('calibration',10),('holdout',20)]:
   session=FootballSession(id=uuid.uuid4(),title=f'PROTOCOL ONLY {split}',created_by=owner.id)
   db.add(session);await db.flush()
   source=('synthetic numerical protocol source '+str(uuid.uuid4())).encode()
   key=f'protocol/{uuid.uuid4()}.bin';path=local_object_path('raw-video',key);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(source)
   sha=hashlib.sha256(source).hexdigest()
   video=Video(id=uuid.uuid4(),filename=f'PROTOCOL ONLY {split}.bin',storage_uri=f'local://raw-video/{key}',content_sha256=sha,size_bytes=len(source),practice_session_id=session.id,capture_regime='fixed_sideline',metadata_={'protocol_fixture':True})
   db.add(video);await db.flush();features=[]
   calibration=FieldCalibration(id=uuid.uuid4(),video_id=video.id,homography=[1,0,0,0,1,0,0,0,1],confidence=.95,confidence_threshold=.7,analytics_safe=True,reason_codes=[],provenance={'source':'human','protocol_fixture':True})
   db.add(calibration);await db.flush()
   for index in range(count):
    clip=Clip(id=uuid.uuid4(),video_id=video.id,start_time=index*5,end_time=index*5+4,is_reviewed=True,capture_regime='fixed_sideline',calibration_version_id=calibration.id,provenance={'source':'real','protocol_fixture':True})
    db.add(clip);await db.flush()
    label=Label(id=uuid.uuid4(),clip_id=clip.id,label_type='coverage_shell',label_value={'coverage':'cover_1' if index%2 else 'cover_3'},source='human',annotated_by=owner.id)
    db.add(label)
    graph={'schema_version':1,'node_ids':[f'p{j}' for j in range(5)],'node_features':[[50+j,float(j),.2,12. if index%2 else 2.,3.,0.,0.] for j in range(5)],'edge_index':[[],[]],'edge_features':[]}
    features.append({'clip_id':str(clip.id),'source':'real','analytics_safe':True,'graph':graph,'protocol_fixture':True})
   feature_key=f'protocol/{uuid.uuid4()}/features.jsonl';path=local_object_path('artifacts',feature_key);path.parent.mkdir(parents=True,exist_ok=True);path.write_text('\n'.join(json.dumps(v) for v in features)+'\n')
   token=uuid.uuid4();job=ProcessingJob(id=uuid.uuid4(),video_id=video.id,job_type=JobType.pipeline,status=JobStatus.running,leased_by=os.environ['PROTOCOL_WORKER_ID'],lease_token=token,attempt_count=1,lease_expires_at=datetime.now(UTC)+timedelta(minutes=5))
   db.add(job)
   result['sessions'].append({'session_id':str(session.id),'split':split})
   result['sources'].append({'job_id':str(job.id),'worker_id':os.environ['PROTOCOL_WORKER_ID'],'lease_token':str(token),'attempt_count':1,'feature_artifact_uri':f'local://artifacts/{feature_key}','source_identity':{'schema_version':1,'video_id':str(video.id),'storage_uri':video.storage_uri,'size_bytes':len(source),'registration_fingerprint':sha,'registration_algorithm':'full-sha256','full_sha256':sha}})
  await db.commit();print(json.dumps(result))
asyncio.run(main())
"""
    result = subprocess.run(  # noqa: S603 -- isolated loopback test DB; fixed fixture script
        [_backend_python(), "-c", script],
        cwd=_ROOT.parent / "backend",
        env={
            **os.environ,
            "SECRET_KEY": "integration-secret",
            "PROTOCOL_ADMIN_EMAIL": _ADMIN["email"],
            "PROTOCOL_WORKER_ID": worker_id,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_real_queued_candidate_publishes_proven_labels_and_retry(backend, monkeypatch):  # noqa: F811
    worker_id = f"generic-label-protocol-{uuid.uuid4().hex}"
    monkeypatch.setenv("WORKER_ID", worker_id)
    with httpx.Client(base_url=backend, timeout=60) as client:
        admin_login = client.post("/api/v1/auth/login", json=_ADMIN)
        admin_login.raise_for_status()
        worker_login = client.post("/api/v1/auth/login", json=_WORKER)
        worker_login.raise_for_status()
        admin = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        worker = {"Authorization": f"Bearer {worker_login.json()['access_token']}"}
        registered = client.post(
            "/api/v1/system/workers/register",
            headers=worker,
            json={
                "worker_id": worker_id,
                "device": "cpu",
                "profiles": ["review", "quick", "detailed"],
                "job_types": ["train", "pipeline"],
            },
        )
        registered.raise_for_status()
        fixture = seed(worker_id)
        for source in fixture["sources"]:
            completed = client.patch(
                f"/api/v1/jobs/{source['job_id']}",
                headers=worker,
                json={
                    "status": "succeeded",
                    "worker_id": worker_id,
                    "lease_token": source["lease_token"],
                    "attempt_count": 1,
                    "output_artifacts": {
                        "source_identity": source["source_identity"],
                        "feature_artifact_uri": source["feature_artifact_uri"],
                    },
                },
            )
            completed.raise_for_status()
        manifest_response = client.post(
            "/api/v1/learning/feature-manifests",
            headers=admin,
            json={"name": "PROTOCOL ONLY candidate label trust", "sessions": fixture["sessions"]},
        )
        assert manifest_response.is_success, manifest_response.text
        manifest = manifest_response.json()
        main = _load_worker_main()
        results = []
        previous_job_id = None
        for execution in range(2):
            if execution:
                request = client.post(f"/api/v1/jobs/{previous_job_id}/retry", headers=admin)
            else:
                request = client.post(
                    "/api/v1/learning/train",
                    headers=admin,
                    json={
                        "training_variant": "local_coverage",
                        "training_dataset_id": manifest["training_dataset_id"],
                        "features_uri": manifest["features_uri"],
                    },
                )
            request.raise_for_status()
            job_id = request.json().get("job_id") or request.json()["id"]
            previous_job_id = job_id
            _prioritize(job_id)
            claim = client.post(
                "/api/v1/jobs/claim",
                headers=worker,
                json={"worker_id": worker_id, "lease_seconds": 120, "job_types": ["train"]},
            )
            claim.raise_for_status()
            claim = claim.json()
            assert claim["id"] == job_id
            if execution == 0:
                original_update = main._update_job_status

                def fail_after_labels(
                    identity, status, _claim=claim, _update=original_update, **kwargs
                ):
                    if status == "succeeded":
                        forged = client.patch(
                            f"/api/v1/jobs/{identity}",
                            headers=admin,
                            json={
                                "status": "succeeded",
                                "worker_id": worker_id,
                                "lease_token": _claim["lease_token"],
                                "attempt_count": _claim["attempt_count"],
                                "output_artifacts": kwargs.get("output_artifacts", {}),
                            },
                        )
                        assert forged.status_code == 403, forged.text
                        status = "failed"
                        kwargs["error_message"] = (
                            "Intentional protocol failure after actual label publication"
                        )
                    return _update(identity, status, **kwargs)

                with monkeypatch.context() as patch:
                    patch.setattr(main, "_update_job_status", fail_after_labels)
                    main.process_job(main._db_row_to_job(claim))
            else:
                main.process_job(main._db_row_to_job(claim))
            completed = _poll(
                lambda identity=job_id: client.get(
                    f"/api/v1/jobs/{identity}", headers=admin
                ).json(),
                lambda value: value.get("status") in {"succeeded", "failed"},
            )
            assert completed["status"] == ("succeeded" if execution else "failed"), completed.get(
                "error_message"
            )
            artifacts = completed["output_artifacts"]
            model_id = artifacts["model_version_id"]
            assert len(artifacts["prediction_label_ids"]) == 20
            evaluation = client.post(
                f"/api/v1/learning/candidates/{model_id}/evaluate",
                headers=admin,
                json={"benchmark_id": manifest["benchmark_id"]},
            )
            if execution == 0:
                assert evaluation.status_code == 422, evaluation.text
            else:
                assert evaluation.is_success, evaluation.text
                assert evaluation.json()["baseline_policy_version"] == 1
                assert evaluation.json()["passed"] is True
                promoted = client.post(
                    f"/api/v1/mlops/models/{model_id}/promote",
                    headers=admin,
                    json={"stage": "production"},
                )
                assert promoted.is_success, promoted.text
            results.append(artifacts)
        assert results[0]["artifact_sha256"] == results[1]["artifact_sha256"]
        assert results[0]["model_version_id"] != results[1]["model_version_id"]
        assert set(results[0]["prediction_label_ids"]).isdisjoint(
            results[1]["prediction_label_ids"]
        )
        evidence = os.environ.get("GENERIC_LABEL_HTTP_EVIDENCE_PATH")
        if evidence:
            Path(evidence).write_text(
                json.dumps(
                    {
                        "protocol_fixture_only": True,
                        "football_accuracy_verified": False,
                        "transport": "actual loopback API, owned runtime, claims, CPU fitting, labels, completion, evaluation and promotion",
                        "dataset_id": manifest["training_dataset_id"],
                        "benchmark_id": manifest["benchmark_id"],
                        "models": [
                            {
                                "model_id": item["model_version_id"],
                                "artifact_sha256": item["artifact_sha256"],
                                "prediction_count": len(item["prediction_label_ids"]),
                            }
                            for item in results
                        ],
                        "same_artifact_new_execution_ids": True,
                        "public_api_retry_preserved_dataset_scope": True,
                        "failed_producer_evaluation_status": 422,
                        "ordinary_staff_copied_fence_completion_status": 403,
                        "note": "Scratch database contract only; numerical protocol fixtures never establish real football validation.",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
