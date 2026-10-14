"""Serving-leg tests: registry resolution, artifact caching, offline fallback."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline import model_registry_client as mrc


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mrc.reset_cache()
    monkeypatch.setenv("MODEL_CACHE_DIR", str(tmp_path / "cache"))
    yield
    mrc.reset_cache()


def _version_payload(artifact_uri: str | None) -> list[dict]:
    return [
        {
            "id": "mv-1",
            "model_name": "yolov8m-toledo",
            "version": "2026.07.1",
            "model_type": "detect",
            "artifact_uri": artifact_uri,
            "promoted_stage": "production",
        }
    ]


def test_resolve_returns_none_without_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BACKEND_API_URL", raising=False)
    assert mrc.resolve("detect") is None


def test_resolve_downloads_and_caches_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BACKEND_API_URL", "http://backend.test")
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"fake-weights")

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _version_payload(f"file://{weights}")

    with patch("httpx.get", return_value=resp) as get:
        first = mrc.resolve("detect")
        second = mrc.resolve("detect")

    assert first is not None
    assert first.model_version_id == "mv-1"
    assert first.local_path.is_file()
    assert first.local_path.read_bytes() == b"fake-weights"
    assert "yolov8m-toledo" in str(first.local_path)
    # Per-process cache: one registry query total.
    assert get.call_count == 1
    assert second is first


def test_resolve_without_artifact_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_API_URL", "http://backend.test")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _version_payload(None)
    with patch("httpx.get", return_value=resp):
        assert mrc.resolve("detect") is None


def test_resolve_download_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_API_URL", "http://backend.test")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _version_payload("local://artifacts/models/missing.pt")
    with patch("httpx.get", return_value=resp):
        # storage.download_to_temp raises (object absent) → graceful None.
        assert mrc.resolve("detect") is None


def test_resolve_query_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_API_URL", "http://backend.test")
    with patch("httpx.get", side_effect=ConnectionError("down")):
        assert mrc.resolve("detect") is None


def test_verified_artifact_hash_rejects_modified_production_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND_API_URL", "http://backend.test")
    artifact = tmp_path / "coverage.json"
    artifact.write_bytes(b"approved checkpoint")
    approved_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    version = _version_payload(f"file://{artifact}")[0]
    version["metrics"] = {"artifact_sha256": approved_hash}
    response = MagicMock()
    response.json.return_value = [version]
    with patch("httpx.get", return_value=response):
        resolved = mrc.resolve("coverage")
        assert resolved is not None and resolved.validated
        assert resolved.artifact_sha256 == approved_hash
        # A changed cache can heal from unchanged approved source bytes.
        resolved.local_path.write_bytes(b"changed cached checkpoint")
        mrc.reset_cache()
        healed = mrc.resolve("coverage")
        assert healed is not None and healed.local_path.read_bytes() == b"approved checkpoint"
        # Changing both source and cache cannot inherit the former approval.
        artifact.write_bytes(b"changed source checkpoint")
        healed.local_path.write_bytes(b"changed cached checkpoint")
        mrc.reset_cache()
        assert mrc.resolve("coverage") is None


def test_legacy_production_name_does_not_imply_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND_API_URL", "http://backend.test")
    artifact = tmp_path / "coverage.json"
    artifact.write_bytes(b"legacy checkpoint")
    response = MagicMock()
    response.json.return_value = _version_payload(f"file://{artifact}")
    with patch("httpx.get", return_value=response):
        resolved = mrc.resolve("coverage")
    assert resolved is not None and resolved.validated is False
