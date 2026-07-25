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
    run_pipeline,
)

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def synthetic_video(tmp_path: Path) -> Path:
    """A 3-second 320x240@30fps mp4 with a moving bright square."""
    path = tmp_path / "synthetic.mp4"
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (320, 240)
    )
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
    monkeypatch.setenv("SEGMENTER_VARIANT", "stub")
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


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_full_chain_offline(
    synthetic_video: Path, offline_env: None, tmp_path: Path
) -> None:
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

    with patch(
        "pipeline.stage_ingest.run", side_effect=AssertionError("ingest re-ran")
    ), patch(
        "pipeline.stage_detect.run", side_effect=AssertionError("detect re-ran")
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


def test_backend_coupled_stages_are_dropped(
    synthetic_video: Path, offline_env: None
) -> None:
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
