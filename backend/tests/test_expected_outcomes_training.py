"""Manual expected-outcomes queue contract, without model imports in the API."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.routers.learning import ExpectedOutcomesTrainingCreate, train
from pydantic import ValidationError


def request(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset": {"path": "reviewed.jsonl", "sha256": "a" * 64},
                "evidence": [],
            }
        )
    )
    return ExpectedOutcomesTrainingCreate(
        training_variant="expected_outcomes",
        manifest_path=str(manifest),
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        output_dir=str(tmp_path / "candidate"),
        task="yac",
    )


def database(active=None):
    result = MagicMock()
    result.scalars.return_value.first.return_value = active
    return SimpleNamespace(
        execute=AsyncMock(return_value=result), add=MagicMock(), flush=AsyncMock()
    )


@pytest.mark.asyncio
async def test_queue_preserves_bound_inputs_and_never_promotes(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.learning.require_workload_capacity", lambda _: AsyncMock())
    body = request(tmp_path)
    db = database()
    result = await train(body, db, SimpleNamespace())
    job = db.add.call_args.args[0]
    assert result["training_variant"] == "expected_outcomes"
    assert job.input_artifacts == body.model_dump(exclude_none=True)
    assert job.priority == 0 and str(job.job_type) == "train"
    assert job.training_dataset_id is None
    assert not (tmp_path / "candidate").exists()


@pytest.mark.asyncio
async def test_queue_preserves_worker_paths_without_reading_api_files(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routers.learning.require_workload_capacity", lambda _: AsyncMock())
    body = request(tmp_path).model_copy(
        update={"manifest_path": r"C:\Staff\reviewed.json", "output_dir": r"C:\Staff\candidate"}
    )
    db = database()
    await train(body, db, SimpleNamespace())
    assert db.add.call_args.args[0].input_artifacts["manifest_path"] == r"C:\Staff\reviewed.json"


def test_expected_request_requires_checksum_and_explicit_variant(tmp_path):
    body = request(tmp_path).model_dump()
    with pytest.raises(ValidationError):
        ExpectedOutcomesTrainingCreate(**{**body, "manifest_sha256": "not-a-hash"})
    with pytest.raises(ValidationError):
        ExpectedOutcomesTrainingCreate(**{**body, "task": "coverage"})
    with pytest.raises(ValidationError):
        ExpectedOutcomesTrainingCreate(**{**body, "output_dir": "  "})
