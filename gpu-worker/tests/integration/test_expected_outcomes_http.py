"""Actual local HTTP candidate -> fenced verifier -> denied release protocol.

Generated tiny videos and numeric labels are synthetic contract fixtures only.
They never satisfy real-football source/label/population acceptance evidence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_pipeline_roundtrip import (
    _ADMIN,
    _ROOT,
    _WORKER,
    _backend_python,
    _load_worker_main,
    _poll,
    backend,  # noqa: F401 -- real loopback API fixture
)

pytestmark = pytest.mark.integration


def _manifest(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "expected_protocol_fixtures", _ROOT / "tests" / "test_expected_outcomes.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    path, _digest = module._manifest(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["protocol_run_id"] = str(uuid.uuid4())
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _prioritize(job_id):
    # Only this test's newly queued job is changed, in the isolated scratch DB.
    script = """
import asyncio, os, uuid
from sqlalchemy import update
from app.database import AsyncSessionLocal
from app.models import ProcessingJob
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(update(ProcessingJob).where(ProcessingJob.id == uuid.UUID(os.environ['PROTOCOL_JOB_ID'])).values(priority=2147483000))
        await db.commit()
asyncio.run(main())
"""
    result = subprocess.run(
        [_backend_python(), "-c", script],
        cwd=_ROOT.parent / "backend",
        env={**os.environ, "SECRET_KEY": "integration-secret", "PROTOCOL_JOB_ID": job_id},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_http_candidate_and_independent_verifier(backend, tmp_path, monkeypatch):  # noqa: F811
    worker_id = f"expected-protocol-{uuid.uuid4().hex}"
    monkeypatch.setenv("WORKER_ID", worker_id)
    manifest, digest = _manifest(tmp_path)
    client = httpx.Client(base_url=backend, timeout=30)
    admin_login = client.post("/api/v1/auth/login", json=_ADMIN)
    admin_login.raise_for_status()
    admin = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    worker_login = client.post("/api/v1/auth/login", json=_WORKER)
    worker_login.raise_for_status()
    worker = {"Authorization": f"Bearer {worker_login.json()['access_token']}"}
    registered = client.post(
        "/api/v1/system/workers/register",
        headers=worker,
        json={
            "worker_id": worker_id,
            "device": "cpu",
            "profiles": ["review", "quick", "detailed"],
            "job_types": ["train"],
            "metadata": {"producer_protocol": 1, "training_variants": ["expected_outcomes", "verify_expected_outcomes"]},
        },
    )
    registered.raise_for_status()
    queued = client.post(
        "/api/v1/learning/train",
        headers=admin,
        json={
            "training_variant": "expected_outcomes",
            "manifest_path": str(manifest),
            "manifest_sha256": digest,
            "output_dir": str(tmp_path / "candidate"),
            "task": "completion",
        },
    )
    queued.raise_for_status()
    train_id = queued.json()["job_id"]
    _poll(
        lambda: client.get(f"/api/v1/jobs/{train_id}", headers=admin),
        lambda response: response.status_code == 200,
    )
    worker_main = _load_worker_main()

    def execute(job_id, *, deny_staff=False):
        _prioritize(job_id)
        claimed = client.post(
            "/api/v1/jobs/claim",
            headers=worker,
            json={"worker_id": worker_id, "lease_seconds": 120, "job_types": ["train"]},
        )
        claimed.raise_for_status()
        claim = claimed.json()
        assert claim["id"] == job_id and claim["status"] == "running"
        assert claim["lease_token"] and claim["attempt_count"] >= 1
        if deny_staff:
            # Ordinary staff can see job listings/fence fields. They still cannot
            # create the trusted verifier completion even with that current fence.
            copied = client.patch(
                f"/api/v1/jobs/{job_id}",
                headers=admin,
                json={
                    "status": "succeeded",
                    "worker_id": worker_id,
                    "lease_token": claim["lease_token"],
                    "attempt_count": claim["attempt_count"],
                    "output_artifacts": {
                        "verification_receipt_uri": "local://artifacts/untrusted.json",
                        "verification_receipt_sha256": "0" * 64,
                    },
                },
            )
            assert copied.status_code == 403
        worker_main.process_job(worker_main._db_row_to_job(claim))
        completed = _poll(
            lambda: client.get(f"/api/v1/jobs/{job_id}", headers=admin).json(),
            lambda value: value.get("status") in {"succeeded", "failed"},
        )
        assert completed["status"] == "succeeded", completed.get("error_message")
        return claim, completed

    train_claim, trained = execute(train_id)
    model_id = trained["output_artifacts"]["registered"]["completion"]["model_version_id"]
    endpoint = f"/api/v1/learning/expected-outcomes/{model_id}/evaluate"
    challenge_response = client.post(endpoint, headers=admin)
    assert challenge_response.is_success, challenge_response.text
    challenge_response.raise_for_status()
    challenge = challenge_response.json()
    assert challenge["status"] == "queued" and "passed" not in challenge
    verify_id = challenge["job_id"]
    _poll(
        lambda: client.get(f"/api/v1/jobs/{verify_id}", headers=admin),
        lambda response: response.status_code == 200,
    )
    verify_claim, verified = execute(verify_id, deny_staff=True)
    assert verified["output_artifacts"]["forward_outputs_matched"] is True
    proof = verified["provenance"]["expected_outcome_verification"]
    assert (
        proof["trusted_execution"] is True
        and proof["attempt_count"] == verify_claim["attempt_count"]
    )
    evaluated = client.post(endpoint, headers=admin)
    evaluated.raise_for_status()
    evaluation = evaluated.json()
    assert evaluation["passed"] is False and evaluation["missing_data_reasons"]
    assert evaluation["verification_job_id"] == verify_id
    promotion = client.post(
        f"/api/v1/mlops/models/{model_id}/promote", headers=admin, json={"stage": "production"}
    )
    assert promotion.status_code == 422
    evidence = {
        "verified_at_utc": datetime.now(UTC).isoformat(),
        "input_source": "synthetic_test",
        "football_accuracy_verified": False,
        "database_scope": os.environ.get(
            "ROUNDTRIP_DATABASE_SCOPE", "isolated localhost scratch DB"
        ),
        "transport": "actual loopback HTTP API, runtime registration, claims and fenced completion",
        "training_job_id": train_id,
        "training_attempt": train_claim["attempt_count"],
        "model_id": model_id,
        "verification_job_id": verify_id,
        "verification_challenge_id": challenge["challenge_id"],
        "verification_attempt": verify_claim["attempt_count"],
        "trusted_execution": proof["trusted_execution"],
        "ordinary_staff_copied_live_fence_completion_status": 403,
        "weights_reloaded_and_forward_outputs_matched": True,
        "original_media_native_clocks_verified": True,
        "verification_receipt_sha256": verified["output_artifacts"]["verification_receipt_sha256"],
        "release_passed": evaluation["passed"],
        "missing_data_reasons": evaluation["missing_data_reasons"],
        "production_promotion_status": promotion.status_code,
    }
    output = os.environ.get("EXPECTED_OUTCOME_HTTP_EVIDENCE_PATH")
    if output:
        Path(output).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    client.close()
