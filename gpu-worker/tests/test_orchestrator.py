"""Orchestrator tests — full stub-mode chain on a tiny synthetic video.

Covers the P1 acceptance contract:
  * stage order (video-scoped once, clip-scoped per clip),
  * offline mode (no BACKEND_API_URL → synthesized records, no HTTP),
  * artifact-ledger writes and resume (completed stages are not re-run),
  * detection slicing per clip window,
  * summary shape consumed by the CLI and the worker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pipeline.detector_models import StubDetector
from pipeline.orchestrator import (
    DirArtifactSink,
    NullArtifactSink,
    PipelineContext,
    _run_clip_stage,
    _run_video_stage,
    run_pipeline,
)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def synthetic_video(tmp_path: Path) -> Path:
    """A 3-second 320x240@30fps mp4 with a moving bright square."""
    path = tmp_path / "synthetic.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (320, 240))
    assert writer.isOpened(), "cv2 could not open VideoWriter for mp4"
    for i in range(90):
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        x = 10 + (i * 2) % 280
        frame[100:140, x : x + 30] = (0, 255, 0)
        writer.write(frame)
    writer.release()
    return path


@pytest.fixture()
def offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Force offline + local storage + deterministic stub adapters."""
    from pipeline import model_router

    select_model = model_router.select_device_model

    def routed_variant(stage: str, priority: int) -> str:
        return "stub" if stage == "segment" else select_model(stage, priority)

    monkeypatch.setattr(
        model_router,
        "select_device_model",
        routed_variant,
    )
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setattr("pipeline.storage.LOCAL_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setattr("pipeline.backend.BACKEND_API_URL", "")


def _stub_detectors() -> Any:
    """Patch player/ball detector builders to the deterministic stub."""
    stub = StubDetector(n_players=4)
    return (
        patch("pipeline.stage_detect.build_player_detector", return_value=stub),
        patch("pipeline.stage_detect.build_ball_detector", return_value=None),
    )


STAGES = ["ingest", "segment", "calibrate", "detect", "track", "events", "labels", "metrics"]


@pytest.mark.parametrize("period", [True, False])
def test_render_receives_actual_effective_staff_snapshot(monkeypatch, period):
    ctx = PipelineContext(video_id="vid", input_uri="file:///film.mp4", job_id="job")
    ctx.video_path = Path("/film.mp4")
    clip = {"id": "clip", "start_time": 0.0, "end_time": 1.0}
    reviewed = {"offensive_formation": {"formation": "Pistol"}}
    ctx.effective_annotations = {"clip": {
        "label_data": {"offensive_formation": {"formation": "CLIP_METADATA_GUESS"}},
        "human_labels": [{"source": "human", "annotated_by": "cf128509-d42d-4532-8c4d-67e44150d391",
                          "label_type": "offensive_formation", "label_value": reviewed["offensive_formation"]}],
    }}
    ctx.clip_artifacts = {"clip": {"labels": {"labels": [
        {"source": "human", "label_type": "offensive_formation",
         "label_value": {"formation": "RAW_GUESS"}},
    ]}}}
    monkeypatch.setattr("pipeline.lightweight_config.use_period_renderer", lambda _: period)
    with patch("renderer.period_renderer.run", return_value={}) as quick, patch(
        "pipeline.stage_render.run", return_value={}
    ) as full:
        _run_clip_stage("render", ctx, clip)
    rendered = quick if period else full
    assert rendered.call_args.kwargs["reviewed_label_data"] == reviewed


@pytest.mark.parametrize("reviewed", [False, True])
def test_label_replay_cannot_launder_raw_clip_metadata(
    synthetic_video, offline_env, tmp_path, monkeypatch, reviewed
):
    import uuid

    from pipeline import orchestrator

    clip = {"id": "clip-review", "start_time": 0.0, "end_time": 1.0,
            "annotation_revision": 3,
            "label_data": {"offensive_formation": {"formation": "MACHINE_METADATA"}}}
    if reviewed:
        clip["human_labels"] = [{"id": str(uuid.uuid4()), "source": "human",
                                 "annotated_by": str(uuid.uuid4()),
                                 "label_type": "offensive_formation",
                                 "label_value": {"formation": "Pistol"}}]
    monkeypatch.setattr(orchestrator, "_run_video_stage", lambda *_: {"clips": [dict(clip)]})
    monkeypatch.setattr(orchestrator, "_run_clip_stage", lambda *_: {"labels": [
        {"label_type": "offensive_formation", "source": "heuristic",
         "label_value": {"formation": "RAW_GUESS"}},
    ]})
    sink = DirArtifactSink(tmp_path / "reviewed-labels")
    result = run_pipeline("video", str(synthetic_video), stages=["segment", "labels"],
                          artifact_sink=sink, revisions={"effective_annotations": [clip]})
    assert result["failed_clip_stages"] == []
    labels = sink.load("labels", "clip-review")["labels"]
    assert labels == [{"label_type": "offensive_formation",
                       "label_value": {"formation": "Pistol" if reviewed else "RAW_GUESS"},
                       "source": "human" if reviewed else "heuristic"}]



def test_registered_source_attestation_precedes_dependent_stages(synthetic_video, offline_env):
    from pipeline.media_identity import content_identity

    source = content_identity(synthetic_video)
    uri = synthetic_video.as_uri()
    calls = []
    result = run_pipeline(
        "registered-source",
        uri,
        stages=["ingest"],
        lineage={
            "source_registration": {
                "storage_uri": uri,
                "content_sha256": source["source_sha256"],
                "size_bytes": source["source_bytes"],
            }
        },
        progress_cb=lambda stage, clip_id, state, extra: calls.append((stage, state, extra)),
    )
    assert calls[0][:2] == ("source_identity", "verified")
    assert calls[0][2]["source_identity"] == result["source_identity"]
    assert result["source_identity"]["full_sha256"] == source["source_sha256"]


def test_angle_calibration_and_tracking_use_clip_specific_regimes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pipeline import stage_calibrate, stage_track

    ctx = PipelineContext(video_id="vid-angles", input_uri="file:///game.mp4", job_id="job")
    ctx.video_path = Path("/game.mp4")
    ctx.clips = [
        {
            "id": "side",
            "view": "sideline",
            "capture_regime": "fixed_sideline",
            "start_time": 3.0,
            "end_time": 14.0,
        },
        {
            "id": "end",
            "view": "endzone",
            "capture_regime": "fixed_endzone",
            "start_time": 14.0,
            "end_time": 27.0,
        },
    ]
    calls: list[tuple[str, float, float]] = []

    def calibrate(video_id, video_path, job_id, regime, variant, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((regime, kwargs["start_time"], kwargs["end_time"]))
        return {
            "analytics_safe": True,
            "capture_regime": regime,
            "homography": [float(len(calls))] * 9,
        }

    monkeypatch.setattr(stage_calibrate, "_calibrate", calibrate)
    artifacts = _run_video_stage("calibrate", ctx)
    assert calls == [
        ("fixed_sideline", 3.0, 14.0),
        ("fixed_endzone", 14.0, 27.0),
    ]

    tracked = {}
    monkeypatch.setattr(stage_track, "run", lambda *args, **kwargs: tracked.update(kwargs) or {})
    _run_clip_stage("track", ctx, ctx.clips[1])

    assert artifacts["capture_regime"] == "multi_view"
    assert tracked["homography"] == [2.0] * 9
    assert tracked["capture_regime"] == "fixed_endzone"
    assert tracked["analytics_safe"] is True


@pytest.mark.parametrize(
    ("variant", "expected_execution"),
    [
        ("calib-numeral-dlt", "numeral-dlt"),
        ("calib-hough-dlt", "classical-line-fit"),
    ],
)
def test_angle_calibration_dispatches_and_records_routed_variant(
    monkeypatch, variant, expected_execution
) -> None:
    from pipeline import model_router, stage_calibrate

    ctx = PipelineContext(
        video_id="vid-routed",
        input_uri="file:///game.mp4",
        priority=10,
        job_id="job",
    )
    ctx.video_path = Path("/game.mp4")
    ctx.clips = [
        {
            "id": "side",
            "view": "sideline",
            "capture_regime": "fixed_sideline",
            "start_time": 0.0,
            "end_time": 2.0,
        }
    ]
    calls = []
    monkeypatch.setattr(model_router, "select_device_model", lambda stage, priority: variant)
    monkeypatch.setattr("pipeline.field_model.get_field_detector", lambda: object())
    monkeypatch.setattr(
        stage_calibrate,
        "_calibrate_broadcast",
        lambda *args, **kwargs: (
            calls.append("numeral") or {"analytics_safe": True, "homography": None}
        ),
    )
    monkeypatch.setattr(
        stage_calibrate,
        "_calibrate",
        lambda *args, **kwargs: (
            calls.append("classical") or {"analytics_safe": True, "homography": [1.0] * 9}
        ),
    )

    artifacts = _run_video_stage("calibrate", ctx)

    assert artifacts["model_routing"] == {"calibrate": variant}
    assert artifacts["model_execution"] == {"calibrate": expected_execution}
    assert calls == ["numeral" if expected_execution == "numeral-dlt" else "classical"]


def test_partial_calibration_safety_survives_tracking_gaps(monkeypatch) -> None:
    ctx = PipelineContext(video_id="vid-partial", input_uri="file:///game.mp4")
    ctx.calibration = {
        "analytics_safe": True,
        "calibration_series": [
            {"frame_number": 0, "analytics_safe": True},
            {"frame_number": 3, "analytics_safe": False},
        ],
    }
    points = [
        {"frame_number": 0, "field_x": 10.0, "field_y": 2.0},
        {"frame_number": 3},
    ]
    ctx.clip_artifacts["clip"] = {"track": {"tracklets": [{"track_points": points}]}}
    captured = {}
    monkeypatch.setattr(
        "pipeline.stage_metrics.run",
        lambda *args, **kwargs: captured.update(analytics_safe=args[3]) or {},
    )

    _run_clip_stage("metrics", ctx, {"id": "clip"})

    assert captured["analytics_safe"] is True
    assert "field_x" not in points[1] and "field_y" not in points[1]


def test_edited_multi_angle_film_disables_shared_spatial_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = PipelineContext(video_id="video", input_uri="film.mp4")
    monkeypatch.setattr(
        "pipeline.stage_segment.run",
        lambda *_a, **_k: {
            "clips": [{"id": "sideline"}, {"id": "endzone"}],
            "game_film": {"edited": True, "sideline_views": 1, "endzone_views": 1},
        },
    )
    _run_video_stage("segment", ctx)

    assert ctx.spatial_analytics_disabled
    calibration = _run_video_stage("calibrate", ctx)
    assert calibration["skipped"] is True
    assert calibration["analytics_safe"] is False
    assert ctx.homography is None and not ctx.calibration_series

    manual = PipelineContext(
        video_id="manual-video",
        input_uri="film.mp4",
        capture_regime="fixed_sideline",
        lineage={
            "existing_clips": [
                {
                    "id": "sideline",
                    "boundary_source": "manual",
                    "capture_regime": "fixed_sideline",
                    "label_data": {"view": "sideline"},
                },
                {
                    "id": "endzone",
                    "boundary_source": "manual",
                    "capture_regime": "fixed_endzone",
                    "label_data": {"view": "endzone"},
                },
            ]
        },
    )
    _run_video_stage("segment", manual)
    assert manual.spatial_analytics_disabled
    assert _run_video_stage("calibrate", manual)["skipped"] is True


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_full_chain_offline(synthetic_video: Path, offline_env: None, tmp_path: Path) -> None:
    calls: list[tuple[str, str | None, str]] = []

    def progress(stage: str, clip_id: str | None, status: str, extra: dict) -> None:
        calls.append((stage, clip_id, status))

    p1, p2 = _stub_detectors()
    with p1, p2:
        summary = run_pipeline(
            "vid-1",
            str(synthetic_video),
            priority=10,
            job_id="job-1",
            stages=STAGES,
            progress_cb=progress,
            artifact_sink=NullArtifactSink(),
        )

    assert summary["video_id"] == "vid-1"
    assert summary["pipeline_mode"] == "same_session"
    assert summary["clip_count"] >= 1
    assert summary["fps"] == pytest.approx(30.0, abs=1.0)

    # Video stages ran once, in order, before any clip stage.
    video_started = [s for (s, c, st) in calls if c is None and st == "started"]
    assert video_started == ["ingest", "segment", "calibrate", "detect"]

    # The stub segmenter's notional 120 s boundaries leave the tail clips
    # beyond this 3 s video (empty by design); the first clip must carry
    # tracklets from the stub detector chain.
    assert summary["clips"][0]["tracklet_count"] > 0
    assert sum(c["tracklet_count"] for c in summary["clips"]) > 0

    # No clip-stage failures in the stub chain.
    assert summary["failed_clip_stages"] == []


def test_ledger_resume_skips_completed_stages(
    synthetic_video: Path, offline_env: None, tmp_path: Path
) -> None:
    sink = DirArtifactSink(tmp_path / "artifacts")

    p1, p2 = _stub_detectors()
    with p1, p2:
        first = run_pipeline(
            "vid-2",
            str(synthetic_video),
            priority=10,
            stages=STAGES,
            artifact_sink=sink,
        )

    ledger_files = {p.name for p in (tmp_path / "artifacts").glob("*.json")}
    assert {"ingest.json", "segment.json", "calibrate.json", "detect.json"} <= ledger_files

    # Second run: ingest must not execute again — resume from the ledger.
    calls: list[tuple[str, str | None, str, dict]] = []

    def progress(stage: str, clip_id: str | None, status: str, extra: dict) -> None:
        calls.append((stage, clip_id, status, extra))

    with (
        patch("pipeline.stage_ingest.run", side_effect=AssertionError("ingest re-ran")),
        patch("pipeline.stage_detect.run", side_effect=AssertionError("detect re-ran")),
    ):
        second = run_pipeline(
            "vid-2",
            str(synthetic_video),
            priority=10,
            stages=STAGES,
            progress_cb=progress,
            artifact_sink=sink,
        )

    resumed = [(s, st) for (s, c, st, extra) in calls if extra.get("resumed")]
    assert ("ingest", "succeeded") in resumed
    assert ("detect", "succeeded") in resumed
    assert second["clip_count"] == first["clip_count"]


def test_clip_detection_slicing(synthetic_video: Path, offline_env: None) -> None:
    """Track receives only the frames inside each clip's window."""
    seen_windows: list[tuple[str, list[int]]] = []

    from pipeline import stage_track

    real_track = stage_track.run

    def spy_track(clip_id: str, detections: dict, fps: float, job_id: str, **kw: Any) -> dict:
        seen_windows.append((clip_id, sorted(int(f) for f in detections)))
        return real_track(clip_id, detections, fps, job_id, **kw)

    p1, p2 = _stub_detectors()
    with p1, p2, patch("pipeline.stage_track.run", side_effect=spy_track):
        summary = run_pipeline(
            "vid-3",
            str(synthetic_video),
            priority=10,
            stages=["ingest", "segment", "calibrate", "detect", "track"],
        )

    assert summary["clip_count"] == len(seen_windows)
    fps = summary["fps"]
    by_clip = {c["id"]: c for c in summary["clips"]}
    for clip_id, frames in seen_windows:
        clip = by_clip[clip_id]
        lo = int(float(clip["start_time"]) * fps)
        hi = int(float(clip["end_time"]) * fps)
        assert all(lo <= f <= hi for f in frames), (clip_id, lo, hi, frames[:5])


def test_backend_coupled_stages_are_dropped(synthetic_video: Path, offline_env: None) -> None:
    p1, p2 = _stub_detectors()
    with p1, p2:
        summary = run_pipeline(
            "vid-4",
            str(synthetic_video),
            priority=10,
            stages=["ingest", "segment", "embeddings", "self_scout", "workload_rollup"],
        )
    # Aggregate stages are excluded, so only the two video stages report.
    assert set(summary["stage_timings"]) <= {"ingest", "segment"}


def test_offline_records_have_ids(offline_env: None) -> None:
    from pipeline import backend

    clip = backend.create_clip("vid-x", start_time=0.0, end_time=4.0)
    assert clip["_offline"] is True and clip["id"]
    tr = backend.create_tracklet("clip-x", start_frame=0, end_frame=10, track_points=[])
    assert tr["_offline"] is True and tr["id"]


def test_cli_smoke(synthetic_video: Path, offline_env: None, tmp_path: Path) -> None:
    """`python -m pipeline run` end-to-end on the synthetic clip."""
    from pipeline.__main__ import main

    out_dir = tmp_path / "cli-out"
    p1, p2 = _stub_detectors()
    with p1, p2:
        code = main(
            [
                "run",
                "--input",
                str(synthetic_video),
                "--out",
                str(out_dir),
                "--stages",
                ",".join(STAGES),
            ]
        )
    assert code == 0
    summary_path = out_dir / synthetic_video.stem / "summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text())
    assert summary["clip_count"] >= 1
    assert (out_dir / synthetic_video.stem / "artifacts" / "detect.json").is_file()


def test_unrecognized_angles_do_not_disable_calibration_for_the_whole_game(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx = PipelineContext(video_id="video", input_uri="film.mp4")
    ctx.video_path = tmp_path / "film.mp4"
    clips = [
        {
            "id": "a",
            "start_time": 2.5,
            "end_time": 19.0,
            "capture_regime": "fixed_sideline",
            "label_data": {"view": "sideline"},
        },
        {
            "id": "b",
            "start_time": 19.0,
            "end_time": 28.0,
            "capture_regime": "fixed_endzone",
            "label_data": {"view": "endzone"},
        },
        {
            "id": "c",
            "start_time": 31.0,
            "end_time": 40.0,
            "capture_regime": "drone_follow",
            "label_data": {"view": "unknown"},
        },
    ]
    monkeypatch.setattr(
        "pipeline.stage_segment.run",
        lambda *_a, **_k: {"clips": clips, "game_film": {"edited": True, "sideline_views": 1}},
    )
    calibrated: list[str] = []

    def fake_calibrate(_video, _path, _job, regime, _variant, **window):
        calibrated.append(regime)
        return {"analytics_safe": True, "capture_regime": regime}

    monkeypatch.setattr("pipeline.stage_calibrate._calibrate", fake_calibrate)
    _run_video_stage("segment", ctx)
    calibration = _run_video_stage("calibrate", ctx)

    assert calibration.get("skipped") is not True
    assert calibrated == ["fixed_sideline", "fixed_endzone"]
    per_clip = calibration["per_clip_calibration"]
    assert per_clip["a"]["analytics_safe"] and per_clip["b"]["analytics_safe"]
    assert per_clip["c"]["analytics_safe"] is False


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_default_routing_uses_the_numeral_fit_when_a_field_model_is_installed(
    monkeypatch, device
) -> None:
    from pipeline import model_router, stage_calibrate

    monkeypatch.setenv("PIPELINE_DEVICE", device)
    monkeypatch.delenv("MODEL_ROUTING_CONFIG", raising=False)
    model_router.reload_routing()
    ctx = PipelineContext(video_id="vid", input_uri="file:///game.mp4", priority=0, job_id="job")
    ctx.video_path = Path("/game.mp4")
    ctx.clips = [
        {
            "id": "side",
            "view": "sideline",
            "capture_regime": "fixed_sideline",
            "start_time": 0.0,
            "end_time": 2.0,
        },
    ]
    calls = []
    monkeypatch.setattr("pipeline.field_model.get_field_detector", lambda: object())
    monkeypatch.setattr(
        stage_calibrate,
        "_calibrate_broadcast",
        lambda *a, **k: calls.append("numeral") or {"analytics_safe": True, "homography": None},
    )
    monkeypatch.setattr(
        stage_calibrate,
        "_calibrate",
        lambda *a, **k: calls.append("classical") or {"analytics_safe": True, "homography": None},
    )

    artifacts = _run_video_stage("calibrate", ctx)

    assert calls == ["numeral"]
    assert artifacts["model_execution"] == {"calibrate": "numeral-dlt"}


def test_system_rewrites_of_an_annotated_clip_keep_checkpoints(
    synthetic_video: Path, offline_env: None, tmp_path: Path
) -> None:
    # Every run relinks the official play-by-play and writes new playback
    # proxies into the clip record; only a staff edit (annotation_revision)
    # may throw the finished stages away.
    from pipeline import stage_ingest

    sink = DirArtifactSink(tmp_path / "artifacts")
    annotated = {
        "id": "clip-1",
        "annotation_revision": 1,
        "start_time": 0.0,
        "end_time": 1.0,
        "human_events": [],
        "label_data": {"cfbd": {"match_score": 4.5}},
        "storage_uri": "s3://clips/attempts/a/clip.mp4",
    }
    rewritten = {
        **annotated,
        "label_data": {"cfbd": {"match_score": 5.0}},
        "storage_uri": "s3://clips/attempts/b/clip.mp4",
    }
    edited = {**rewritten, "annotation_revision": 2}
    real_ingest = stage_ingest.run
    ran: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        ran.append(1)
        return real_ingest(*args, **kwargs)

    with patch("pipeline.stage_ingest.run", side_effect=counted):
        for record in (annotated, rewritten, edited):
            run_pipeline(
                "vid-9",
                str(synthetic_video),
                priority=10,
                stages=["ingest", "segment"],
                artifact_sink=sink,
                revisions={"effective_annotations": [record]},
            )
            ran.append(0)
    # First run computes; the system rewrite resumes; the staff edit recomputes.
    assert ran == [1, 0, 0, 1, 0]


def test_annotation_identity_keeps_only_what_staff_change() -> None:
    from pipeline.orchestrator import annotation_identity

    record = {
        "id": "b",
        "annotation_revision": 3,
        "start_time": 1.0,
        "end_time": 2.0,
        "human_events": [{"event_type": "snap"}],
        "label_data": {"cfbd": {}},
        "storage_uri": "x",
    }
    assert annotation_identity([record, {"id": "a"}]) == [
        {
            "id": "a",
            "annotation_revision": None,
            "start_time": None,
            "end_time": None,
            "human_events": None,
            "removed_events": None,
            "human_labels": None,
            "actor_kind_labels": None,
        },
        {
            "id": "b",
            "annotation_revision": 3,
            "start_time": 1.0,
            "end_time": 2.0,
            "human_events": [{"event_type": "snap"}],
            "removed_events": None,
            "human_labels": None,
            "actor_kind_labels": None,
        },
    ]
    removed = {**record, "removed_events": [{"id": "old", "provenance": {"state": "superseded"}}]}
    assert annotation_identity([removed]) != annotation_identity([record])
    human = {**record, "human_labels": [{"id": "reviewed-label"}]}
    assert annotation_identity([human]) != annotation_identity([record])


def test_normal_expected_stage_exports_current_review_scope(
    synthetic_video, offline_env, tmp_path, monkeypatch
):
    import runpy

    from pipeline import model_registry_client, orchestrator, stage_expected_outcomes, storage

    fixtures = runpy.run_path(str(Path(__file__).with_name("test_expected_outcomes.py")))
    event, tracks = fixtures["_observed_release"]()
    monkeypatch.setattr(storage, "LOCAL_STORAGE_ROOT", str(tmp_path / "export-store"))
    monkeypatch.setattr(model_registry_client, "resolve", lambda _: None)
    clip = {
        "id": "clip-export",
        "start_time": 0,
        "end_time": 3,
        "annotation_revision": 7,
        "label_data": {"_observation_revision": 4},
    }
    monkeypatch.setattr(orchestrator, "_run_video_stage", lambda name, ctx: {"clips": [clip]})

    def stage(name, ctx, current):
        assert name == "expected_outcomes"
        return stage_expected_outcomes.run(
            current["id"],
            ctx.job_id,
            10,
            tracklets=tracks,
            events=[event],
            analytics_safe=True,
            persist=False,
            lineage={
                "session_id": "session",
                "recording_id": ctx.video_id,
                "canonical_play_id": current["id"],
                "media_sha256": ctx.lineage["source_sha256"],
            },
        )

    monkeypatch.setattr(orchestrator, "_run_clip_stage", stage)
    result = run_pipeline(
        "video-export",
        str(synthetic_video),
        job_id="job-export",
        priority=10,
        stages=["segment", "expected_outcomes"],
        artifact_sink=NullArtifactSink(),
        revisions={"effective_annotations": [clip]},
    )
    descriptor = result["expected_feature_export"]
    path = storage.download_to_temp(descriptor["artifact_uri"])
    import json

    document = json.loads(path.read_bytes())
    assert document["clip_revisions"] == {
        "clip-export": {"annotation_revision": 7, "observation_revision": 4}
    }
    assert set(document["feature_snapshots"]) == {"clip-export:completion"}
    assert len(json.dumps(descriptor)) < 2048
