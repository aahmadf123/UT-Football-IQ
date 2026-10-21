"""Synthetic protocol fixtures test release enforcement, never model accuracy."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services.expected_outcomes import review_release
from app.storage import local_object_path
from fastapi import HTTPException


def write_artifact(name, body):
    path = local_object_path("artifacts", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(body, sort_keys=True).encode()
    path.write_bytes(encoded)
    return f"local://artifacts/{name}", hashlib.sha256(encoded).hexdigest()


def release_fixture(tmp_path, monkeypatch, *, full=False, task="completion"):
    from app.config import get_settings
    from app.services.expected_outcomes import CLOCKS, FEATURES, POPULATIONS, TASK_TYPES, _metrics

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    weight_uri, weights_hash = write_artifact("fixture.bin", {"fixture": "not a trained model"})
    rows, artifacts, media, predictions = [], [], [], []
    sources = []
    splits = [("train", 30, "train"), ("calibration", 10, "calibration")]
    splits.extend(("holdout", 20, f"holdout-{i}") for i in range(12 if full else 1))
    binary = task in {"completion", "pressure"}
    for split, count, group in splits:
        media_hash = hashlib.sha256(f"fixture-media-{group}".encode()).hexdigest()
        media.append({"path": f"{group}.mp4", "kind": "media", "sha256": media_hash})
        if full and split == "holdout":
            category = ("practice", "coach_game", "television")[int(group.rsplit("-", 1)[1]) // 4]
            sources.append(
                {
                    "group_id": group,
                    "category": category,
                    "independence_confirmed": True,
                    "reviewer_ids": ["fixture-source-reviewer"],
                    "recording_ids": [group],
                    "media_sha256": [media_hash],
                }
            )
        for index in range(count):
            outcome = int(index % 5 < 3) if full and split == "holdout" else index % 2
            row = {
                "schema_version": 1,
                "source": "real",
                "capture_regime": "fixed_sideline",
                "task": task,
                "feature_version": "expected-outcomes-2",
                "eligible_population": POPULATIONS[task],
                "subject": {"scope": "team", "reason": "contextual_front_prediction"}
                if task == "pressure"
                else {
                    "scope": "player",
                    "role": {"completion": "QB", "rushing_gain": "rusher", "yac": "receiver"}[task],
                    "tracklet_id": f"fixture-{group}-{index}-actor",
                },
                "lineage": {
                    "session_id": group,
                    "recording_id": group,
                    "canonical_play_id": f"{group}-{index}",
                    "media_sha256": media_hash,
                },
                "observation_clock": {"event": CLOCKS[task], "timestamp_seconds": 2.0},
                "split": split,
                "features": {
                    name: {
                        "value": float(1 + index % 2) if unit == "count" else float(index % 2),
                        "unit": unit,
                        "qualified": True,
                        "observed_at_seconds": 2.0,
                    }
                    for name, unit in FEATURES[task]
                },
                "target": {
                    "value": outcome if binary else outcome * 10,
                    "source": "human",
                    "reviewed": True,
                    "reviewer_id": "fixture-reviewer",
                    "reviewed_at": "2026-10-21T12:00:00Z",
                },
            }
            if full and split == "holdout" and index < 4:
                row["target"].update(
                    double_labeled=True,
                    adjudicated=True,
                    reviewer_ids=["fixture-reviewer", "fixture-reviewer-two"],
                )
            feature = {k: v for k, v in row.items() if k not in {"target", "split"}}
            feature = {
                **feature,
                "features": {k: dict(v) for k, v in row["features"].items()},
                "kind": "expected_outcome_features",
            }
            annotation = {
                "schema_version": 1,
                "kind": "expected_outcome_annotation",
                "task": row["task"],
                "lineage": row["lineage"],
                "observation_clock": row["observation_clock"],
                "target": dict(row["target"]),
            }
            for kind, body in (("features", feature), ("annotation", annotation)):
                content = json.dumps(body, sort_keys=True)
                digest = hashlib.sha256(content.encode()).hexdigest()
                artifacts.append(
                    {
                        "kind": kind,
                        "sha256": digest,
                        "content": content,
                        "path": f"{group}-{index}.{kind}.json",
                    }
                )
                if kind == "features":
                    for value in row["features"].values():
                        value["evidence_sha256"] = digest
                else:
                    row["target"]["annotation_sha256"] = digest
            rows.append(row)
            if split == "holdout":
                predictions.append(
                    {
                        "task": task,
                        "weights_sha256": weights_hash,
                        "feature_version": row["feature_version"],
                        "eligible_population": row["eligible_population"],
                        "lineage": row["lineage"],
                        "subject": row["subject"],
                        "observation_clock": row["observation_clock"],
                        "feature_evidence_sha256": [
                            v["evidence_sha256"] for v in row["features"].values()
                        ],
                        "capture_regime": "fixed_sideline",
                        "suppressed": False,
                        "value": (
                            (0.9 if outcome else 0.1) if full else (0.98 if outcome else 0.02)
                        )
                        if binary
                        else outcome * 10.0,
                    }
                )
    dataset_text = "\n".join(json.dumps(row, sort_keys=True) for row in rows)
    dataset_hash = hashlib.sha256(dataset_text.encode()).hexdigest()
    manifest_text = json.dumps(
        {
            "schema_version": 1,
            "dataset": {"path": "rows.jsonl", "sha256": dataset_hash},
            "evidence": [
                *media,
                *[{k: a[k] for k in ("kind", "sha256", "path")} for a in artifacts],
            ],
            "release_population": {"source_groups": sources},
        },
        sort_keys=True,
    )
    identity = {
        "manifest_sha256": hashlib.sha256(manifest_text.encode()).hexdigest(),
        "dataset_sha256": dataset_hash,
    }
    audit = {
        "schema_version": 1,
        "source": "real",
        "task": task,
        "weights_sha256": weights_hash,
        "dataset_identity": identity,
        "inputs": {
            "manifest": manifest_text,
            "dataset": dataset_text,
            "artifacts": artifacts,
            "verified_media_sha256": [m["sha256"] for m in media],
            "media_sources": [{"path": m["path"], "sha256": m["sha256"]} for m in media],
        },
        "predictions": predictions,
    }
    truth = [row["target"]["value"] for row in rows if row["split"] == "holdout"]
    mean = 0.5 if binary else 5.0
    report = {
        "task": task,
        "split": "holdout",
        "weights_sha256": weights_hash,
        "eligible_population": POPULATIONS[task],
        "count": len(truth),
        "dataset_identity": identity,
        "metrics": _metrics(truth, [p["value"] for p in predictions], binary),
        "baseline": {
            "definition": "empirical_training_population_mean",
            "training_mean": mean,
            "metrics": _metrics(truth, [mean] * len(truth), binary),
        },
    }
    report_uri, report_hash = write_artifact("evaluation.json", report)
    release_uri, release_hash = write_artifact("release.json", audit)
    model = SimpleNamespace(
        id=uuid.uuid4(),
        model_type=TASK_TYPES[task],
        artifact_uri=weight_uri,
        metrics={
            "artifact_sha256": weights_hash,
            "report_uri": report_uri,
            "report_sha256": report_hash,
            "release_evidence_uri": release_uri,
            "release_evidence_sha256": release_hash,
        },
    )
    return model, audit, report


def test_release_recomputes_losses_against_real_bound_bytes(tmp_path, monkeypatch):
    model, _audit, _report = release_fixture(tmp_path, monkeypatch)
    result = review_release(model)
    assert (
        result["passed"] is False
    )  # Tiny protocol fixtures never satisfy the full release population.
    assert "requires_240_distinct_heldout_canonical_plays" in result["missing_data_reasons"]
    assert result["metrics"]["brier"] == pytest.approx(0.0004)
    assert result["baseline"]["metrics"]["brier"] == pytest.approx(0.25)
    assert result["capture_regimes"] == ["fixed_sideline"]


@pytest.mark.parametrize(
    "mutate",
    [
        "wrong_task",
        "wrong_weights",
        "wrong_report",
        "leaking_split",
        "no_data",
        "poor_calibration",
        "forged_diagnostics",
        "fractional_diagnostics",
    ],
)
def test_release_refuses_wrong_or_unqualified_evidence(tmp_path, monkeypatch, mutate):
    model, audit, report = release_fixture(tmp_path, monkeypatch)
    if mutate == "wrong_task":
        model.model_type = "expected_yac"
    elif mutate == "wrong_weights":
        audit["weights_sha256"] = "0" * 64
    elif mutate == "wrong_report":
        report["metrics"]["brier"] = 0.0
    elif mutate == "no_data":
        audit["predictions"] = []
    elif mutate == "forged_diagnostics":
        report["metrics"]["error_distribution"]["bins"][0]["count"] += 1
    elif mutate == "fractional_diagnostics":
        report["metrics"]["confusion_matrix"]["counts"][0][0] += 1e-9
    elif mutate == "leaking_split":
        audit["inputs"]["dataset"] = audit["inputs"]["dataset"].replace(
            '"session_id": "holdout-0"', '"session_id": "train"'
        )
    else:
        for prediction in audit["predictions"]:
            prediction["value"] = 0.51
    model.metrics["release_evidence_uri"], model.metrics["release_evidence_sha256"] = (
        write_artifact("release.json", audit)
    )
    model.metrics["report_uri"], model.metrics["report_sha256"] = write_artifact(
        "evaluation.json", report
    )
    with pytest.raises(HTTPException):
        review_release(model)


class FakeSession:
    def __init__(self):
        self.rows = {}

    def add(self, row):
        key = (
            getattr(row, "key", None) or getattr(row, "id", None) or getattr(row, "worker_id", None)
        )
        self.rows[(type(row), key)] = row

    async def get(self, cls, key):
        return self.rows.get((cls, key))

    async def flush(self):
        pass


async def verification_fixture(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.models import JobStatus, ProcessingJob, SystemSetting, WorkerRuntime
    from app.services.expected_outcomes import VERIFIER_VERSION, evaluate_expected_outcomes

    monkeypatch.setenv("SEED_WORKER_EMAIL", "protocol-worker@example.test")
    monkeypatch.setattr("app.workload.require_workload_capacity", lambda _: AsyncMock())
    model, _audit, _report = release_fixture(tmp_path, monkeypatch)
    get_settings.cache_clear()
    db = FakeSession()
    actor = SimpleNamespace(id=uuid.uuid4(), email="protocol-analyst@example.test")
    requested = await evaluate_expected_outcomes(db, model, actor)
    assert requested["status"] == "queued" and "passed" not in requested
    job = await db.get(ProcessingJob, uuid.UUID(requested["job_id"]))
    challenge = await db.get(SystemSetting, f"learning:verification:{requested['challenge_id']}")
    worker = SimpleNamespace(id=uuid.uuid4(), email="protocol-worker@example.test")
    db.add(WorkerRuntime(worker_id="protocol-worker", registered_by=worker.id))
    job.status, job.leased_by, job.lease_token, job.attempt_count = (
        JobStatus.running,
        "protocol-worker",
        uuid.uuid4(),
        1,
    )
    job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    binding = challenge.value["binding"]
    receipt = {
        "schema_version": 1,
        "kind": "expected_outcome_verification_receipt",
        "verifier_version": VERIFIER_VERSION,
        "challenge_id": requested["challenge_id"],
        "verification_job_id": str(job.id),
        "model_id": str(model.id),
        "worker_id": "protocol-worker",
        "attempt_count": 1,
        "task": "completion",
        "forward_outputs_matched": True,
        "source_media_verified": True,
        "source_clock_verified": True,
        "dataset_identity": binding["dataset_identity"],
        "feature_version": "expected-outcomes-2",
        "prediction_count": 20,
        "recomputed_predictions_sha256": "a" * 64,
        **{
            key: binding[key]
            for key in ("artifact_sha256", "report_sha256", "release_evidence_sha256")
        },
    }
    uri, digest = write_artifact("receipt.json", receipt)
    output = {"verification_receipt_uri": uri, "verification_receipt_sha256": digest}
    return db, model, actor, worker, job, output


@pytest.mark.asyncio
async def test_only_completed_fenced_independent_job_can_record_evaluation(tmp_path, monkeypatch):
    from app.models import JobStatus
    from app.services.expected_outcomes import (
        bind_verification_completion,
        evaluate_expected_outcomes,
    )

    db, model, actor, worker, job, output = await verification_fixture(tmp_path, monkeypatch)
    await bind_verification_completion(
        db,
        job,
        worker,
        output,
        worker_id=job.leased_by,
        lease_token=job.lease_token,
        attempt_count=job.attempt_count,
    )
    job.status, job.output_artifacts = JobStatus.succeeded, output
    job.leased_by, job.lease_token, job.lease_expires_at = None, None, None
    evaluated = await evaluate_expected_outcomes(db, model, actor)
    assert evaluated["verification_job_id"] == str(job.id)
    assert evaluated["passed"] is False  # Actual full source-population gate remains missing.
    from app.models import SystemSetting

    stored = await db.get(SystemSetting, f"learning:evaluation:{evaluated['id']}")
    assert stored.value == evaluated
    assert sum(map(sum, stored.value["metrics"]["confusion_matrix"]["counts"])) == 20
    assert sum(b["count"] for b in stored.value["metrics"]["error_distribution"]["bins"]) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["ordinary_staff", "stale_attempt", "wrong_challenge", "missing_receipt"]
)
async def test_verification_receipt_cannot_bypass_trusted_worker_boundary(
    tmp_path, monkeypatch, failure
):
    from app.services.expected_outcomes import bind_verification_completion

    db, _model, actor, worker, job, output = await verification_fixture(tmp_path, monkeypatch)
    count = job.attempt_count
    if failure == "ordinary_staff":
        worker = actor
    elif failure == "stale_attempt":
        count -= 1
    elif failure == "wrong_challenge":
        job.input_artifacts = {**job.input_artifacts, "challenge_id": str(uuid.uuid4())}
    else:
        output = {}
    with pytest.raises(HTTPException):
        await bind_verification_completion(
            db,
            job,
            worker,
            output,
            worker_id=job.leased_by,
            lease_token=job.lease_token,
            attempt_count=count,
        )


@pytest.mark.asyncio
async def test_raw_audit_or_copied_receipt_cannot_promote(tmp_path, monkeypatch):
    from app.models import JobStatus
    from app.services.expected_outcomes import validate_expected_promotion

    db, model, _actor, _worker, job, output = await verification_fixture(tmp_path, monkeypatch)
    job.status, job.output_artifacts = JobStatus.succeeded, output
    evaluation = {
        **review_release(model),
        "verification_challenge_id": model.metrics["verification_challenge_id"],
    }
    with pytest.raises(HTTPException):
        await validate_expected_promotion(db, model, evaluation)


@pytest.mark.parametrize("task", ["completion", "rushing_gain", "yac", "pressure"])
def test_full_protocol_population_and_task_gates_are_executable(tmp_path, monkeypatch, task):
    model, _audit, _report = release_fixture(tmp_path, monkeypatch, full=True, task=task)
    result = review_release(model)
    assert result["population"]["canonical_plays"] == 240
    assert result["population"]["independent_groups"] == 12
    assert (
        result["passed"] is True
    )  # Statistical protocol only; no trusted receipt or promotion here.


def test_s3_artifacts_use_owned_store_and_bound_hashes(monkeypatch):
    import io

    from app.services import expected_outcomes

    encoded = b'{"test":"protocol-only"}'
    calls = []
    client = SimpleNamespace(
        get_object=lambda **kwargs: calls.append(kwargs) or {"Body": io.BytesIO(encoded)}
    )
    monkeypatch.setattr(expected_outcomes, "get_s3_client", lambda: client)
    monkeypatch.setattr(expected_outcomes, "physical_bucket", lambda _: "owned-artifacts")
    digest = hashlib.sha256(encoded).hexdigest()
    assert expected_outcomes._read("s3://artifacts/expected/report.json", digest) == {
        "test": "protocol-only"
    }
    assert calls == [{"Bucket": "owned-artifacts", "Key": "expected/report.json"}]
    with pytest.raises(ValueError, match="checksum"):
        expected_outcomes._read("s3://artifacts/expected/report.json", "0" * 64)
    with pytest.raises(ValueError, match="namespace"):
        expected_outcomes._read("s3://unowned/expected/report.json", digest)


def test_one_source_cannot_count_as_twelve_independent_groups():
    from app.services.expected_outcomes import _population

    groups = [
        {
            "group_id": f"g{i}",
            "category": "practice",
            "independence_confirmed": True,
            "reviewer_ids": ["fixture-source-reviewer"],
            "recording_ids": ["same-recording"],
            "media_sha256": ["a" * 64],
        }
        for i in range(12)
    ]
    audit = {"inputs": {"manifest": json.dumps({"release_population": {"source_groups": groups}})}}
    with pytest.raises(ValueError, match="cannot count"):
        _population(audit, {"holdout": []})
