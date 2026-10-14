"""Real codec checks for short, bounded, browser-compatible play overlays."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from pipeline.runtime import PipelineInterrupted, job_context
from renderer import period_renderer
from worker.media_process import run_checked


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "recording.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (224, 128))
    assert writer.isOpened()
    for frame_number in range(90):
        writer.write(np.full((128, 224, 3), 60 + frame_number, dtype=np.uint8))
    writer.release()
    return path


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="Local media tools required"
)
def test_overlay_uses_only_play_frames_and_browser_codec(source: Path, tmp_path: Path) -> None:
    output = tmp_path / "play.mp4"
    period_renderer._render_reduced(source, output, [], [], True, 30, frame_start=30, frame_end=45)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    assert stream["codec_name"] == "h264"
    assert int(stream["nb_frames"]) == 15
    assert float(stream["duration"]) == pytest.approx(0.5, abs=0.02)
    # Absolute recording frames remain the tracking clock; output playback
    # starts at zero and contains the original frame30, rather than frame0.
    capture = cv2.VideoCapture(str(output))
    ok, frame = capture.read()
    capture.release()
    assert ok
    assert float(frame[40:80, 80:160].mean()) > 80


def test_period_overlay_exports_separately_from_clean_playback(
    source: Path, tmp_path: Path
) -> None:
    def write(_source: Path, target: Path, *_args, **_kwargs) -> None:
        target.write_bytes(b"encoded")

    with (
        patch.object(period_renderer, "_render_reduced", side_effect=write),
        patch.object(
            period_renderer.object_store, "upload_file", return_value="local://overlays/play.mp4"
        ),
        patch("pipeline.backend.patch_clip_storage_uri") as patch_clip,
    ):
        result = period_renderer.run(
            "clip", source, [], [], False, 30, frame_start=30, frame_end=45
        )
    patch_clip.assert_not_called()
    assert result["source_start_time_seconds"] == 1
    assert result["overlay_start_time_seconds"] == 0


def test_missing_encoder_is_reported_without_promoting_unplayable_video(tmp_path: Path) -> None:
    with (
        patch("worker.media_process.run_checked", side_effect=FileNotFoundError),
        pytest.raises(RuntimeError, match="ffmpeg"),
    ):
        period_renderer._transcode_h264(tmp_path / "raw.mp4", tmp_path / "output.mp4")


def test_media_command_obeys_job_control() -> None:
    import sys

    with job_context(control=lambda: "cancel"), pytest.raises(PipelineInterrupted):
        run_checked([sys.executable, "-c", "import time; time.sleep(30)"])
