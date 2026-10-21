"""Detection rate: ~30 looks per second on a GPU, cheap default on CPU."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.stage_detect import detection_frame_step


def test_gpu_detects_at_about_thirty_per_second(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DETECT_FRAME_STEP", raising=False)
    assert detection_frame_step(29.97, "cuda") == 1  # practice drone clip
    assert detection_frame_step(59.94, "cuda") == 2  # 60 fps game film
    assert detection_frame_step(120.0, "cuda:0") == 4


def test_cpu_keeps_the_cheaper_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DETECT_FRAME_STEP", raising=False)
    assert detection_frame_step(29.97, "cpu") == 3


def test_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DETECT_FRAME_STEP", "5")
    assert detection_frame_step(59.94, "cuda") == 5


def test_gpu_batches_detector_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.stage_detect import detection_batch_size

    monkeypatch.delenv("DETECT_BATCH", raising=False)
    assert detection_batch_size("cuda:0") == 16
    assert detection_batch_size("cpu") == 1
    monkeypatch.setenv("DETECT_BATCH", "4")
    assert detection_batch_size("cuda:0") == 4


def test_batched_detection_matches_frame_by_frame(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from pipeline.detection.official_suppressor import OfficialSuppressor
    from pipeline.detector_models import StubDetector
    from pipeline.stage_detect import _detect

    source = tmp_path / "sixty.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=s=128x72:r=60000/1001", "-frames:v", "41",
                    "-c:v", "libx264", str(source)], check=True, capture_output=True)
    calls: list[int] = []

    class Counting(StubDetector):
        def detect_batch(self, frames):  # type: ignore[no-untyped-def]
            calls.append(len(frames))
            return super().detect_batch(frames)

    def run(batch: str) -> dict:
        calls.clear()
        monkeypatch.setenv("DETECT_FRAME_STEP", "2")
        monkeypatch.setenv("DETECT_BATCH", batch)
        return _detect(source, Counting(), None, OfficialSuppressor(enabled=False))

    single = run("1")
    assert calls == [1] * 21
    batched = run("8")
    assert calls == [8, 8, 5]  # 21 detected frames, the remainder flushed at the end
    assert batched["detections"] == single["detections"]
    assert sorted(map(int, batched["detections"])) == list(range(0, 41, 2))
    assert batched["total_frames"] == 41
