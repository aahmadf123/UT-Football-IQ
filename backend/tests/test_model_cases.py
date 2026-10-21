"""Frozen protocol case reports -> stored evidence -> authenticated category reads."""

# ruff: noqa: F811
import json
import os
import uuid
from pathlib import Path

import pytest
from app.models import SystemSetting
from app.services.expected_outcomes import review_release
from app.services.model_cases import store_cases
from app.services.model_diagnostics import case_document, heldout_cases
from test_clip_edits import workspace  # noqa: F401
from test_expected_outcome_release import release_fixture, write_artifact


def case_fixture(tmp_path, monkeypatch):
    model, audit, report = release_fixture(tmp_path, monkeypatch)
    rows = [json.loads(line) for line in audit["inputs"]["dataset"].splitlines() if line]
    heldout = [r for r in rows if r["split"] == "holdout"]
    report["heldout_diagnostics"] = heldout_cases(
        heldout, [p["value"] for p in audit["predictions"]], report["baseline"]["training_mean"]
    )
    uri, digest = write_artifact("evaluation.json", report)
    model.metrics.update(report_uri=uri, report_sha256=digest)
    return model, audit, report


@pytest.mark.parametrize(
    "change",
    ["observed", "predicted", "source", "clock", "category", "tiny_prediction", "tiny_baseline"],
)
def test_case_bundle_is_independently_recomputed_not_just_hash_checked(
    tmp_path, monkeypatch, change
):
    model, _, report = case_fixture(tmp_path, monkeypatch)
    cases = report["heldout_diagnostics"]["cases"]
    if change == "observed":
        cases[0]["observed"] = 99.0
    if change == "predicted":
        cases[0]["predicted"] = 0.5
    if change == "source":
        cases[0]["lineage"]["recording_id"] = "foreign"
    if change == "clock":
        cases[0]["observation_clock"]["timestamp_seconds"] += 0.1
    if change == "tiny_prediction":
        cases[0]["predicted"] += 1e-12
    if change == "tiny_baseline":
        cases[0]["baseline"] += 1e-12
    if change == "category":
        cases[0]["film_category"] = "television"
    report["heldout_diagnostics"] = case_document(cases)
    uri, digest = write_artifact("evaluation.json", report)
    model.metrics.update(report_uri=uri, report_sha256=digest)
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="case diagnostics"):
        review_release(model)


async def test_actual_producer_stored_case_api_filter_download_and_tampering(
    workspace, tmp_path, monkeypatch
):
    client, db, _ = workspace
    model, _, _ = case_fixture(tmp_path, monkeypatch)
    result = review_release(model)
    document = result.pop("_case_document")
    identity = uuid.uuid4()
    result["id"] = str(identity)
    from app.deps import get_current_user
    from app.main import app

    actor = app.dependency_overrides[get_current_user]()
    db.add(SystemSetting(key=f"learning:evaluation:{identity}", value=result, updated_by=actor.id))
    store_cases(db, str(identity), str(model.id), result["artifact_sha256"], document, actor.id)
    await db.flush()
    response = await client.get(
        f"/api/v1/learning/evaluations/{identity}/cases", params={"limit": 5}
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["selected_count"] == page["canonical_play_count"] == 20 and len(page["cases"]) == 5
    assert page["release_qualification_inherited"] is False and result["passed"] is False
    filtered = await client.get(
        f"/api/v1/learning/evaluations/{identity}/cases",
        params={"dimension": "observed_outcome", "category": "1"},
    )
    assert filtered.status_code == 200, filtered.text
    selected = filtered.json()
    assert selected["selected_count"] == 10 and selected["small_sample"] is True
    assert {case["baseline"] for case in selected["cases"]} == {0.5}  # never refit to selected y=1
    assert selected["baseline_metrics"]["brier"] == 0.25
    assert selected["known_source_group_count"] == 1
    assert all(case["film"] is None and case["film_unavailable_reason"] for case in page["cases"])
    download = await client.get(
        f"/api/v1/learning/evaluations/{identity}/cases",
        params={"dimension": "observed_outcome", "category": "1", "download": True},
    )
    assert download.status_code == 200 and len(download.json()["cases"]) == 10
    assert (
        download.headers["content-disposition"]
        == f'attachment; filename="heldout-cases-{identity}.json"'
    )
    assert (
        await client.get(
            f"/api/v1/learning/evaluations/{identity}/cases",
            params={"dimension": "unknown", "category": "x"},
        )
    ).status_code == 422
    full = (await client.get(f"/api/v1/learning/evaluations/{identity}/cases")).json()
    if os.environ.get("WRITE_RELIABILITY_CASE_FIXTURE") == "1":
        path = (
            Path(__file__).resolve().parents[2]
            / "frontend/src/components/__fixtures__/model-reliability-cases-producer.json"
        )
        path.write_text(
            json.dumps(
                {
                    "fixture_notice": (
                        "Synthetic protocol fixture, not real football validation. "
                        "Producer -> stored record -> actual authenticated API response."
                    ),
                    "evaluation": result,
                    "page": full,
                    "selected": selected,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    stored = await db.get(SystemSetting, f"learning:cases:{identity}")
    changed = json.loads(json.dumps(stored.value))
    changed["document"]["cases"][0]["predicted"] = 0.999
    stored.value = changed
    await db.flush()
    assert (await client.get(f"/api/v1/learning/evaluations/{identity}/cases")).status_code == 422


def test_confirmed_category_membership_and_canonical_dedup(tmp_path, monkeypatch):
    _, audit, _ = case_fixture(tmp_path, monkeypatch)
    row = next(
        json.loads(line)
        for line in audit["inputs"]["dataset"].splitlines()
        if json.loads(line)["split"] == "holdout"
    )
    group = {
        "group_id": row["lineage"]["session_id"],
        "category": "practice",
        "independence_confirmed": True,
        "reviewer_ids": ["fixture"],
        "recording_ids": [row["lineage"]["recording_id"]],
        "media_sha256": [row["lineage"]["media_sha256"]],
    }
    assert (
        heldout_cases([row], [0.5], 0.5, source_groups=[group])["cases"][0]["film_category"]
        == "practice"
    )
    group["media_sha256"] = ["b" * 64]
    assert (
        heldout_cases([row], [0.5], 0.5, source_groups=[group])["cases"][0]["film_category"]
        == "unknown"
    )
    with pytest.raises(ValueError, match="alternate views"):
        heldout_cases([row, row], [0.5, 0.5], 0.5)


@pytest.mark.parametrize("task", ["completion", "rushing_gain", "yac", "pressure"])
def test_actual_worker_fitted_report_cases_match_independent_server_exactly(
    worker_python, tmp_path, monkeypatch, task
):
    import subprocess
    from types import SimpleNamespace

    from app.config import get_settings
    from app.services.expected_outcomes import TASK_TYPES
    from app.storage import local_object_path

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    get_settings.cache_clear()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    code = """
import json,runpy,sys
from pathlib import Path
from training.expected_outcomes import train_manifest
fixture=runpy.run_path('tests/test_expected_outcomes.py')
path,sha=fixture['_manifest'](Path(sys.argv[1]),task=sys.argv[2])
print(json.dumps(train_manifest(path,sha,Path(sys.argv[1])/'candidate')['models'][sys.argv[2]]))
"""
    result = subprocess.run(  # noqa: S603 -- fixed local worker command and protocol fixtures
        [worker_python, "-c", code, str(inputs), task],
        cwd=Path(__file__).resolve().parents[2] / "gpu-worker",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    artifact = json.loads(result.stdout.splitlines()[-1])
    registered = {}
    for key in ["checkpoint", "report", "release_evidence"]:
        destination = local_object_path("artifacts", f"worker-{task}/{key}.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Path(artifact[f"{key}_path"]).read_bytes())
        registered["artifact_uri" if key == "checkpoint" else f"{key}_uri"] = (
            f"local://artifacts/worker-{task}/{key}.json"
        )
    model = SimpleNamespace(
        id=uuid.uuid4(),
        model_type=TASK_TYPES[task],
        artifact_uri=registered.pop("artifact_uri"),
        metrics={
            **registered,
            **{
                key: artifact[key]
                for key in ["artifact_sha256", "report_sha256", "release_evidence_sha256"]
            },
        },
    )
    checked = review_release(model)
    assert (
        checked["heldout_diagnostics"]["sha256"]
        == artifact["report"]["heldout_diagnostics"]["sha256"]
    )
    assert checked["_case_document"] == artifact["report"]["heldout_diagnostics"]
    assert checked["passed"] is False
