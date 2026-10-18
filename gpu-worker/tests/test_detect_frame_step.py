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
