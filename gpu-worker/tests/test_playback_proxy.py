"""Clean play media remains available when optional inference cannot run."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from pipeline import backend, orchestrator, playback_proxy, storage
from pipeline.orchestrator import DirArtifactSink, run_pipeline
from pipeline.playback_proxy import PlaybackProxySession, _StreamingEncoder
from pipeline.runtime import PipelineInterrupted, job_context
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


@pytest.mark.parametrize("during_write", [True, False])
def test_streaming_encoder_interrupts_and_reaps_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_write: bool
) -> None:
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def sleeping_process(*_args, **kwargs):
        # Scope the encoder fake to ffmpeg; thermal control can independently
        # probe nvidia-smi on GPU-equipped developer computers.
        if _args and _args[0][0] != "ffmpeg":
            return real_popen(*_args, **kwargs)
        process = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=kwargs["stdin"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            bufsize=kwargs["bufsize"],
        )
        processes.append(process)
        return process

    monkeypatch.setattr(playback_proxy.subprocess, "Popen", sleeping_process)
    checks = 0

    def control() -> str | None:
        nonlocal checks
        checks += 1
        return "cancel" if checks >= (2 if during_write else 1) else None

    with pytest.raises(PipelineInterrupted):
        with job_context(control=control):
            with _StreamingEncoder(tmp_path / "clip.mp4", (2048, 2048), 30) as encoder:
                if during_write:
                    encoder.write(np.zeros((1024, 1024, 3), dtype=np.uint8))

    assert processes[0].poll() is not None


def test_proxy_has_a_keyframe_every_second(tmp_path: Path) -> None:
    from pipeline.playback_proxy import _StreamingEncoder

    command = _StreamingEncoder(tmp_path / "clip.mp4", (224, 128), 30).command
    assert command[command.index("-g") + 1] == "30"
    assert command[command.index("-keyint_min") + 1] == "30"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Local ffmpeg required")
def test_clean_proxy_preserves_available_source_audio(source: Path, tmp_path: Path) -> None:
    with_audio = tmp_path / "source-audio.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-itsoffset", "0.2", "-i", str(source), "-f", "lavfi", "-i",
        "sine=frequency=440:duration=3", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-shortest", str(with_audio),
    ], check=True, capture_output=True)
    session = PlaybackProxySession(with_audio, VideoTimeline.from_path(with_audio))
    try:
        result = session.run("audio-clip", 1, 2)
        audio = result["playback"]["audio"]
        assert audio["status"] == "preserved" and audio["passed"]
        assert audio["duration_drift_seconds"] <= 0.1
        assert audio["source_seek_seconds"] == pytest.approx(1.2, abs=0.01)
        assert result["playback"]["alignment_validation"]["passed"]
        assert result["playback"]["artifact_fingerprint"] in result["playback_uri"]
    finally:
        session.close()
