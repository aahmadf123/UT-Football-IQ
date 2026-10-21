"""Storage/registry protocol fixtures only; these do not validate football models."""

import copy
import json
from pathlib import Path

import pytest

from training import expected_outcomes


@pytest.mark.parametrize("change", ["report", "evidence", "same"])
def test_candidate_binding_preserves_historical_artifacts(tmp_path, monkeypatch, change):
    from pipeline import backend, storage

    artifacts = []
    for index in range(2):
        directory = tmp_path / str(index)
        directory.mkdir()
        checkpoint = directory / "checkpoint.json"
        checkpoint.write_text('{"protocol_fixture":true}', encoding="utf-8")
        report = {
            "protocol_fixture": True,
            "revision": index if change == "report" else 0,
            "heldout_diagnostics": {
                "schema_version": 1,
                "case_count": 0,
                "sha256": "a" * 64,
                "groups": {},
                "cases": [],
            },
        }
        report_path = directory / "evaluation.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        evidence_path = directory / "release-evidence.json"
        evidence_path.write_text(
            json.dumps(
                {"protocol_fixture": True, "revision": index if change == "evidence" else 0}
            ),
            encoding="utf-8",
        )
        artifacts.append(
            {
                "checkpoint_path": str(checkpoint),
                "report_path": str(report_path),
                "release_evidence_path": str(evidence_path),
                "artifact_sha256": expected_outcomes.file_sha256(checkpoint),
                "report_sha256": expected_outcomes.file_sha256(report_path),
                "release_evidence_sha256": expected_outcomes.file_sha256(evidence_path),
                "report": report,
            }
        )
    pending = iter(artifacts)
    monkeypatch.setattr(
        expected_outcomes,
        "train_manifest",
        lambda *args, **kwargs: {"models": {"completion": next(pending)}},
    )
    uploaded = {}
    registrations = []

    def upload(path, key):
        uri = f"local://models/{key}"
        uploaded[uri] = Path(path).read_bytes()
        return uri

    def register(**kwargs):
        registrations.append(copy.deepcopy(kwargs))
        return {"id": kwargs["model_version_id"]}

    monkeypatch.setattr(storage, "upload_file", upload)
    monkeypatch.setattr(backend, "register_model_version", register)
    monkeypatch.setattr(backend, "BACKEND_API_URL", "http://backend.test")
    monkeypatch.setattr(expected_outcomes, "check_control", lambda: None)
    inputs = {"manifest_path": "protocol.json", "manifest_sha256": "b" * 64, "output_dir": "new"}
    expected_outcomes.train_expected_outcomes_job(inputs, "first-protocol-job")
    first = registrations[0]
    first_report_uri = first["metrics"]["report_uri"]
    first_evidence_uri = first["metrics"]["release_evidence_uri"]
    first_report_bytes = uploaded[first_report_uri]
    first_evidence_bytes = uploaded[first_evidence_uri]
    expected_outcomes.train_expected_outcomes_job(inputs, "second-protocol-job")
    second = registrations[1]

    assert uploaded[first_report_uri] == first_report_bytes
    assert uploaded[first_evidence_uri] == first_evidence_bytes
    assert first["artifact_uri"] == second["artifact_uri"]
    assert artifacts[0]["report_sha256"] in first_report_uri
    assert artifacts[0]["release_evidence_sha256"] in first_evidence_uri
    assert "cases" not in first["metrics"]["heldout_diagnostics"]
    assert json.loads(uploaded[first_report_uri])["heldout_diagnostics"]["cases"] == []
    if change == "same":
        assert first["model_version_id"] == second["model_version_id"]
        assert first["version"] == second["version"]
        assert first_report_uri == second["metrics"]["report_uri"]
        assert first_evidence_uri == second["metrics"]["release_evidence_uri"]
    else:
        assert first["model_version_id"] != second["model_version_id"]
        assert first["version"] != second["version"]
        assert len(first["version"]) <= 100
