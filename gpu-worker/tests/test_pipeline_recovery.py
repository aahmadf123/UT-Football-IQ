"""Durable pipeline recovery contracts; stage models are replaced explicitly."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import orchestrator, storage
from pipeline.checkpoints import checkpoint_identity
from pipeline.orchestrator import DirArtifactSink, StorageArtifactSink, run_pipeline
from pipeline.runtime import job_context, set_checkpoint_identity, stable_record_id
from tests.test_db_queue_worker import worker_main


def test_native_stop_file_requests_cooperative_worker_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_file = tmp_path / "worker.stop"
    monkeypatch.setattr(worker_main, "_shutdown", False)
    monkeypatch.setenv("LOCAL_APP_STOP_FILE", str(stop_file))
    assert not worker_main._shutdown_requested()
    stop_file.touch()
    assert worker_main._shutdown_requested()
    with patch.object(worker_main.time, "sleep") as idle:
        worker_main._idle_wait(60)
    idle.assert_not_called()


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "film.mp4"
    path.write_bytes(b"source-film-fixture")
    monkeypatch.setenv("PIPELINE_DEVICE", "cpu")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(storage, "LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setattr(orchestrator, "_score_clip_uncertainty", lambda *_: None)
    return path


def video_stage(stage: str, ctx: orchestrator.PipelineContext) -> dict:
    if stage == "segment":
        return {"clips": [{"id": "clip-1", "start_time": 0, "end_time": 2}], "clip_count": 1}
    if stage == "detect":
        return {
            "detections": {},
            "model_provenance": {
                "detect": {"source": "real", "effective_variant": "fixture-model"},
            },
        }
    return {"probe": {"fps": 30}, "capture_regime": "fixed_sideline"}


def test_storage_ledger_is_discoverable_in_a_new_process(source: Path) -> None:
    identity = checkpoint_identity(
        source, video_id="video", profile="review", stages=["ingest"], priority=0
    )
    first = StorageArtifactSink("video")
    first.configure(identity)
    first.write("ingest", {"fps": 24})
    second = StorageArtifactSink("video")
    second.configure(identity)
    assert second.load("ingest") == {"fps": 24}


def test_observation_contract_change_refuses_stale_metric_checkpoint(
    source: Path, monkeypatch
) -> None:
    from pipeline import checkpoints

    options = {
        "video_id": "clock-version",
        "profile": "review",
        "stages": ["metrics"],
        "priority": 0,
    }
    old = checkpoint_identity(source, **options)
    sink = StorageArtifactSink("clock-version")
    sink.configure(old)
    sink.write("metrics", {"time_to_throw": "old-candidate"})
    monkeypatch.setattr(checkpoints, "OBSERVED_METRICS_VERSION", "next-reviewed-clock-contract")
    current = checkpoint_identity(source, **options)
    assert current["fingerprint"] != old["fingerprint"]
    resumed = StorageArtifactSink("clock-version")
    resumed.configure(current)
    assert resumed.load("metrics") is None


def test_reviewed_overlay_policy_refuses_old_burned_in_checkpoint(source: Path, monkeypatch) -> None:
    from pipeline import checkpoints

    options = {"video_id": "render-policy", "profile": "review", "stages": ["render"], "priority": 0}
    with monkeypatch.context() as old_policy:
        old_policy.setattr(checkpoints, "OVERLAY_GENERATOR_VERSION", "pts-overlay-v4")
        old = checkpoint_identity(source, **options)
    sink = StorageArtifactSink("render-policy")
    sink.configure(old)
    sink.write("render", {"overlay_uri": "local://old-raw-label-preview.mp4"})
    current = checkpoint_identity(source, **options)
    assert current["fingerprint"] != old["fingerprint"]
    resumed = StorageArtifactSink("render-policy")
    resumed.configure(current)
    assert resumed.load("render") is None


def test_source_model_and_annotation_changes_invalidate_checkpoints(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weights = tmp_path / "weights.pt"
    weights.write_bytes(b"model-one")
    monkeypatch.setenv("MODEL_DETECT_PATH", str(weights))
    options = dict(video_id="video", profile="quick", stages=["detect"], priority=0)
    first = checkpoint_identity(source, **options)
    weights.write_bytes(b"model-two")
    assert checkpoint_identity(source, **options)["fingerprint"] != first["fingerprint"]
    revised = checkpoint_identity(source, **options, revisions={"correction_revision": 2})
    assert revised["fingerprint"] != checkpoint_identity(source, **options)["fingerprint"]
    source.write_bytes(b"different-film")
    assert checkpoint_identity(source, **options)["source_sha256"] != first["source_sha256"]


def test_corrupt_and_incompatible_checkpoints_are_never_resumed(
    source: Path,
    tmp_path: Path,
) -> None:
    sink = DirArtifactSink(tmp_path / "ledger")
    sink.configure({"fingerprint": "first"})
    sink.write("detect", {"detections": {"1": []}})
    record = json.loads((sink.root / "detect.json").read_text())
    record["payload"]["detections"] = {"42": [{"fake": True}]}
    (sink.root / "detect.json").write_text(json.dumps(record))
    assert sink.load("detect") is None
    sink.write("detect", {"detections": {}})
    sink.configure({"fingerprint": "changed"})
    assert sink.load("detect") is None


@pytest.mark.parametrize(
    "setting",
    [
        "DETECTION_CONF",
        "BALL_DETECTION_CONF",
        "EXPERIMENTAL_FOOTBALL_METRICS",
        "POSE_EXPERIMENTAL_METRICS",
        "OFFICIAL_SUPPRESSION_ENABLED",
        "REGIME_MODEL_PATH",
    ],
)
def test_analysis_configuration_changes_invalidate_checkpoints(
    source: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    options = dict(video_id="video", profile="quick", stages=["detect"], priority=0)
    original = checkpoint_identity(source, **options)
    monkeypatch.setenv(setting, "different")
    assert checkpoint_identity(source, **options)["fingerprint"] != original["fingerprint"]


def test_pause_saves_completed_stages_and_resume_does_not_repeat_them(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = DirArtifactSink(tmp_path / "ledger")
    control = {"action": None}
    called: list[str] = []

    def runner(stage: str, ctx: orchestrator.PipelineContext) -> dict:
        called.append(stage)
        return video_stage(stage, ctx)

    def progress(stage: str, _clip: str | None, status: str, _extra: dict) -> None:
        if stage == "ingest" and status == "succeeded":
            control["action"] = "pause"

    monkeypatch.setattr(orchestrator, "_run_video_stage", runner)
    with job_context(control=lambda: control["action"]):
        paused = run_pipeline(
            "video",
            str(source),
            stages=["ingest", "segment"],
            artifact_sink=sink,
            progress_cb=progress,
        )
    assert paused["result_state"] == "interrupted"
    assert paused["interrupted_action"] == "pause"
    assert called == ["ingest"]
    with job_context():
        resumed = run_pipeline(
            "video",
            str(source),
            stages=["ingest", "segment"],
            artifact_sink=DirArtifactSink(sink.root),
        )
    assert called == ["ingest", "segment"]
    assert resumed["stage_states"]["ingest"]["resumed"] is True


def test_required_inference_failure_preserves_film_without_trusted_results(
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(stage: str, ctx: orchestrator.PipelineContext) -> dict:
        if stage == "detect":
            raise RuntimeError("Real detector weights are unavailable")
        return video_stage(stage, ctx)

    monkeypatch.setattr(orchestrator, "_run_video_stage", runner)
    clip_runner = patch.object(orchestrator, "_run_clip_stage")
    with clip_runner as downstream, job_context():
        result = run_pipeline("video", str(source), stages=["ingest", "segment", "detect", "track"])
    downstream.assert_not_called()
    assert result["result_state"] == "partial"
    assert result["clip_count"] == 1
    assert result["trusted_analysis"] is False
    assert result["failed_video_stages"][0]["stage"] == "detect"


def test_identity_and_labels_enrich_tracks_in_live_and_resumed_pipeline(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator, "_run_video_stage", video_stage)
    observed: list[dict] = []

    def clip_runner(stage: str, ctx: orchestrator.PipelineContext, clip: dict) -> dict:
        if stage == "track":
            return {"tracklets": [{"id": "track-1"}], "tracklet_count": 1}
        if stage == "reid":
            return {"tracklets": [{"id": "track-1", "player_id": "player-7"}]}
        if stage == "labels":
            return {
                "tracklets": [{"id": "track-1", "player_id": "player-7", "team_side": "offense"}]
            }
        observed.extend(ctx.clip_artifacts[clip["id"]]["track"]["tracklets"])
        return {"metric_count": 0}

    monkeypatch.setattr(orchestrator, "_run_clip_stage", clip_runner)
    sink = DirArtifactSink(tmp_path / "ledger")
    stages = ["ingest", "segment", "detect", "track", "reid", "labels", "metrics"]
    with job_context():
        run_pipeline("video", str(source), stages=stages, artifact_sink=sink)
    (sink.root / "metrics.clip-1.json").unlink()
    with job_context():
        run_pipeline("video", str(source), stages=stages, artifact_sink=DirArtifactSink(sink.root))
    assert observed == [
        {"id": "track-1", "player_id": "player-7", "team_side": "offense"},
        {"id": "track-1", "player_id": "player-7", "team_side": "offense"},
    ]


def test_replayed_clips_keep_ids_across_correction_revisions() -> None:
    payload = {"start_time": 1, "end_time": 4, "job_id": "first", "boundary_source": "model"}
    with job_context():
        set_checkpoint_identity("original-analysis", "same-film")
        original = stable_record_id("/api/v1/videos/video/clips", payload)
        set_checkpoint_identity("corrected-analysis", "same-film")
        replayed = stable_record_id("/api/v1/videos/video/clips", {**payload, "job_id": "retry"})
    assert original == replayed


def test_event_write_persists_source_provenance_and_job_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipeline import backend

    monkeypatch.setattr(backend, "BACKEND_API_URL", "")
    with job_context(lease={"job_id": "job", "worker_id": "worker", "lease_token": "token"}):
        event = backend.create_event(
            "clip",
            "snap",
            attributes={
                "source": "synthetic_test",
                "provenance": {"source": "synthetic_test", "validated": False},
            },
        )
    assert event["provenance"]["source"] == "synthetic_test"
    assert event["job_id"] == "job"


def test_worker_acknowledges_pause_with_fenced_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path))
    job = {
        "jobId": "job",
        "videoId": "video",
        "inputUri": "file:///fixture.mp4",
        "leaseToken": "token",
        "attemptCount": 2,
        "inputArtifacts": {"processing_profile": "review"},
    }
    result = {"interrupted_action": "pause", "result_state": "interrupted"}
    with (
        patch.object(orchestrator, "run_pipeline", return_value=result),
        patch.object(worker_main, "_update_job_status") as update,
        patch("pipeline.backend.fetch_annotation_snapshot", return_value=[]),
        patch("pipeline.backend.fetch_roster_for_video", return_value=[]),
    ):
        worker_main.process_pipeline_job(job)
    update.assert_called_once_with("job", "paused", output_artifacts=result)


def test_worker_status_write_includes_attempt_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_main, "BACKEND_API_URL", "http://backend.test")
    monkeypatch.setitem(
        worker_main._CURRENT_LEASES, "job", {"lease_token": "token", "attempt_count": 3}
    )
    with patch("httpx.Client") as client:
        worker_main._update_job_status("job", "paused")
    payload = client.return_value.__enter__.return_value.patch.call_args.kwargs["json"]
    assert payload["lease_token"] == "token"
    assert payload["attempt_count"] == 3


def test_coach_boundaries_survive_reprocessing(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = orchestrator.PipelineContext("video", "file:///unused.mp4")
    clips = [
        {"id": "reviewed", "start_time": 4, "end_time": 9, "boundary_source": "manual"},
        {"id": "other", "start_time": 12, "end_time": 16, "boundary_source": "heuristic"},
    ]
    ctx.lineage = {"existing_clips": clips}
    with patch("pipeline.stage_segment.run", side_effect=AssertionError("replaced coach cuts")):
        result = orchestrator._run_video_stage("segment", ctx)
    assert result["clips"] == clips
    assert result["data_source"] == "manual"


def test_large_job_summary_keeps_database_payload_bounded() -> None:
    result = {
        "stage_states": {f"detect:{i}": {"status": "succeeded"} for i in range(2000)},
        "clips": [{"id": str(i)} for i in range(2000)],
        "clip_count": 2000,
        "failed_clip_stages": [],
        "failed_video_stages": [],
        "checkpoint": {"fingerprint": "run"},
        "result_state": "complete",
        "expected_feature_export": {
            "artifact_uri": "local://artifacts/feature.json",
            "sha256": "a" * 64,
            "row_count": 2,
        },
    }
    with patch.object(
        storage, "upload_bytes", return_value="local://artifacts/full.json"
    ) as upload:
        compact = worker_main._bounded_job_summary(result, "job")
    assert len(json.dumps(compact).encode()) < 48 * 1024
    assert compact["clip_count"] == 2000
    assert compact["expected_feature_export"] == result["expected_feature_export"]
    assert compact["clips_truncated"] is True
    assert compact["summary_artifact_uri"] == "local://artifacts/full.json"
    assert json.loads(upload.call_args.args[0])["clips"] == result["clips"]


def test_interrupted_summary_does_not_publish_after_cancel() -> None:
    result = {
        "stage_states": {f"detect:{i}": {"status": "succeeded"} for i in range(2000)},
        "interrupted_action": "cancel",
        "checkpoint": {"fingerprint": "run"},
    }
    with patch.object(storage, "upload_bytes") as upload:
        compact = worker_main._bounded_job_summary(result, "job", publish=False)
    upload.assert_not_called()
    assert compact["interrupted_action"] == "cancel"


def test_dispatch_nested_artifacts_preserve_profile_and_revisions() -> None:
    job = worker_main._db_row_to_job(
        {
            "id": "job",
            "video_id": "video",
            "job_type": "pipeline",
            "input_artifacts": {
                "inputUri": "local://raw-video/film.mp4",
                "inputArtifacts": {
                    "processing_profile": "detailed",
                    "correction_revision": 7,
                },
            },
        }
    )
    assert job["inputArtifacts"]["processing_profile"] == "detailed"
    assert job["inputArtifacts"]["correction_revision"] == 7


def test_media_outputs_are_immutable_across_attempts(source: Path) -> None:
    with job_context(lease={"job_id": "job", "worker_id": "worker", "lease_token": "first"}):
        first = storage.upload_bytes(b"first", "overlays/clip.mp4")
    with job_context(lease={"job_id": "job", "worker_id": "worker", "lease_token": "second"}):
        second = storage.upload_bytes(b"second", "overlays/clip.mp4")
    assert first != second
    first_path = storage.download_to_temp(first)
    second_path = storage.download_to_temp(second)
    try:
        assert first_path.read_bytes() == b"first"
        assert second_path.read_bytes() == b"second"
    finally:
        first_path.unlink()
        second_path.unlink()


@pytest.mark.parametrize("pipeline_error", [None, RuntimeError("stage unavailable")])
def test_rejected_terminal_attempt_cannot_publish_video_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pipeline_error: Exception | None,
) -> None:
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path))
    job = {
        "jobId": "job",
        "videoId": "video",
        "inputUri": "file:///fixture.mp4",
        "leaseToken": "stale",
        "attemptCount": 1,
        "inputArtifacts": {},
    }
    with (
        patch.object(
            orchestrator,
            "run_pipeline",
            return_value={"result_state": "complete"},
            side_effect=pipeline_error,
        ),
        patch.object(worker_main, "_update_job_status", return_value=False),
        patch("pipeline.backend.fetch_annotation_snapshot", return_value=[]),
        patch("pipeline.backend.fetch_video_context", return_value={}),
        patch("pipeline.backend.fetch_roster_for_video", return_value=[]),
        patch("pipeline.backend.patch_video_status") as video_status,
    ):
        worker_main.process_pipeline_job(job)
    video_status.assert_not_called()


def test_completed_analysis_without_usable_playback_cannot_mark_film_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIPELINE_CHECKPOINT_ROOT", str(tmp_path))
    job = {
        "jobId": "job",
        "videoId": "video",
        "inputUri": "file:///fixture.mp4",
        "leaseToken": "current",
        "attemptCount": 1,
        "inputArtifacts": {},
    }
    with (
        patch.object(
            orchestrator,
            "run_pipeline",
            return_value={
                "result_state": "complete",
                "clip_count": 0,
                "playback_ready": False,
            },
        ),
        patch.object(worker_main, "_update_job_status", return_value=True),
        patch("pipeline.backend.fetch_annotation_snapshot", return_value=[]),
        patch("pipeline.backend.fetch_video_context", return_value={}),
        patch("pipeline.backend.fetch_roster_for_video", return_value=[]),
        patch("pipeline.backend.patch_video_status") as video_status,
    ):
        worker_main.process_pipeline_job(job)
    video_status.assert_called_once_with("video", "uploaded")
