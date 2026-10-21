"""Normal stage snapshots remain reachable even when no outcome model is available."""

import hashlib
import json
import runpy
from pathlib import Path

import pytest

from pipeline import model_registry_client, stage_expected_outcomes, storage
from pipeline.expected_outcomes.data import DataError
from pipeline.expected_outcomes.export import export_feature_snapshots

fixtures = runpy.run_path(str(Path(__file__).with_name("test_expected_outcomes.py")))
_observed_release, snapshot = fixtures["_observed_release"], fixtures["snapshot"]


def normal(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(storage, "LOCAL_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(model_registry_client, "resolve", lambda _: None)
    event, tracks = _observed_release()
    lineage = snapshot()["lineage"]
    stage = stage_expected_outcomes.run(
        "clip",
        "job",
        10,
        tracklets=tracks,
        events=[event],
        lineage=lineage,
        analytics_safe=True,
        persist=False,
    )
    return stage, lineage


def test_normal_unmodeled_stage_uploads_exact_readable_snapshots(monkeypatch, tmp_path):
    stage, lineage = normal(monkeypatch, tmp_path)
    assert stage["predictions"]["completion"]["suppressed"] is True
    descriptor = export_feature_snapshots(
        {"clip": {"expected_outcomes": stage}},
        video_id=lineage["recording_id"],
        job_id="job",
        source_uri="local://raw-video/film.mp4",
        media_sha256=lineage["media_sha256"],
        source_clock_verified=True,
        clip_revisions={"clip": {"annotation_revision": 0, "observation_revision": 0}},
    )
    path = storage.download_to_temp(descriptor["artifact_uri"])
    try:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == descriptor["sha256"]
        document = json.loads(path.read_bytes())
        packet = document["feature_snapshots"]["clip:completion"]
        assert "target" not in packet and "split" not in packet
        assert (
            packet["feature_evidence"]
            == stage["feature_snapshots"]["completion"]["feature_evidence"]
        )
        assert descriptor["row_count"] == 1 and len(json.dumps(descriptor)) < 2048
    finally:
        path.unlink()


def test_export_rejects_changed_feature_evidence_or_source_and_unverified_clock(
    monkeypatch, tmp_path
):
    stage, lineage = normal(monkeypatch, tmp_path)
    kwargs = dict(
        video_id=lineage["recording_id"],
        job_id="job",
        source_uri="local://raw-video/film.mp4",
        media_sha256=lineage["media_sha256"],
        source_clock_verified=True,
        clip_revisions={"clip": {"annotation_revision": 0, "observation_revision": 0}},
    )
    assert (
        export_feature_snapshots(
            {"clip": {"expected_outcomes": stage}}, **{**kwargs, "source_clock_verified": False}
        )
        is None
    )
    with pytest.raises(DataError, match="recording"):
        export_feature_snapshots(
            {"clip": {"expected_outcomes": stage}}, **{**kwargs, "media_sha256": "0" * 64}
        )
    stage["feature_snapshots"]["completion"]["feature_evidence"]["features"][
        "target_distance_to_qb"
    ]["value"] += 1
    with pytest.raises(DataError, match="unchanged"):
        export_feature_snapshots({"clip": {"expected_outcomes": stage}}, **kwargs)
