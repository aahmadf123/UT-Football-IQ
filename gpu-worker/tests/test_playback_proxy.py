"""Clean play media remains available when optional inference cannot run."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from pipeline import backend, orchestrator, storage
from pipeline.orchestrator import DirArtifactSink, run_pipeline
from pipeline.playback_proxy import PlaybackProxySession
from pipeline.runtime import job_context
from pipeline.video_timeline import VideoTimeline


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "film.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (224, 128))
    assert writer.isOpened()
    for number in range(90):
        writer.write(np.full((128, 224, 3), 60 + number, dtype=np.uint8))
    writer.release()
    monkeypatch.setattr(storage, "LOCAL_STORAGE_ROOT", str(tmp_path / "library"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.delenv("PIPELINE_STORAGE_TRANSPORT", raising=False)
    monkeypatch.setattr(backend, "BACKEND_API_URL", "")
    monkeypatch.setattr(orchestrator, "_score_clip_uncertainty", lambda *_: None)
    return path


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Local ffmpeg required")
def test_review_profile_creates_clean_browser_media_without_detector(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def video_stage(stage: str, _ctx) -> dict:
        calls.append(stage)
        if stage == "segment":
            return {
                "clips": [{"id": "first", "start_time": 1, "end_time": 1.5}],
                "clip_count": 1,
                "data_source": "synthetic_test",
            }
        return {"probe": {"fps": 30, "codec_name": "mpeg4"}}

    monkeypatch.setattr(orchestrator, "_run_video_stage", video_stage)
    with job_context():
        result = run_pipeline(
            "video",
            str(source),
            processing_profile="review",
            artifact_sink=DirArtifactSink(tmp_path / "ledger"),
        )
    assert calls == ["ingest", "segment"]
    assert result["playback_ready"] is True
    assert result["trusted_analysis"] is False
    uri = result["clips"][0]["playback_uri"]
    assert uri.startswith("local://clips/")
    path = storage.download_to_temp(uri)
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        assert stream["codec_name"] == "h264"
        assert int(stream["nb_frames"]) == 15
        capture = cv2.VideoCapture(str(path))
        ok, frame = capture.read()
        capture.release()
        assert ok
        # Clean playback has no painted HUD, rectangles or calibration banner.
        assert np.std(frame.mean(axis=2)) < 1.5
        assert frame.mean() > 80
    finally:
        path.unlink()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Local ffmpeg required")
def test_missing_detector_preserves_finished_playback(
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def video_stage(stage: str, _ctx) -> dict:
        order.append(stage)
        if stage == "detect":
            raise RuntimeError("Real detector unavailable")
        if stage == "segment":
            return {"clips": [{"id": "first", "start_time": 0, "end_time": 0.5}], "clip_count": 1}
        return {"probe": {"fps": 30, "codec_name": "mpeg4"}}

    monkeypatch.setattr(orchestrator, "_run_video_stage", video_stage)
    real_run = PlaybackProxySession.run

    def playback(self, *args, **kwargs):
        order.append("playback_proxy")
        return real_run(self, *args, **kwargs)

    monkeypatch.setattr(PlaybackProxySession, "run", playback)
    with job_context():
        result = run_pipeline(
            "video",
            str(source),
            stages=["ingest", "segment", "playback_proxy", "detect"],
            artifact_sink=DirArtifactSink(tmp_path / "ledger"),
        )
    assert order.index("playback_proxy") < order.index("detect")
    assert result["result_state"] == "partial"
    assert result["playback_ready"] is True
    assert result["clips"][0]["playback_uri"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Local ffmpeg required")
def test_clean_proxy_uses_exact_vfr_presentation_mapping(source: Path) -> None:
    # Decoded frame sequence is supplied by the fixture; varying presentation
    # times prove image duplication follows PTS rather than average FPS.
    timestamps = tuple(
        [i * 0.02 for i in range(20)] + [0.4 + (i - 20) * 0.08 for i in range(20, 90)]
    )
    timeline = VideoTimeline(timestamps, timestamps[-1] + 0.08, 15, verified=True)
    session = PlaybackProxySession(source, timeline)
    try:
        with job_context():
            result = session.run("clip", 0.44, 0.94)
        mapping = result["proxy_timeline"]["proxy_to_source_frame"]
        assert mapping[0] == 20
        assert len(mapping) == 15
        assert mapping.count(21) >= 2
        assert result["playback"]["source_start_seconds"] == 0.44
    finally:
        session.close()
