"""Unit tests for gpu-worker/renderer/period_renderer.py and hls_encoder.py.

All tests run without GPU hardware, real video files, or R2 storage.
cv2 and ffmpeg are mocked so the tests are fast and hermetic.

period_renderer covers:
  - Happy path: run() uploads to R2 and returns period_overlay_uri
  - Happy path: _label_value extracts formation correctly
  - Happy path: _label_value returns default when label absent
  - Failure: cv2.VideoCapture failure propagates

hls_encoder covers:
  - Happy path: run() calls ffmpeg, uploads segments, returns URIs
  - Happy path: master playlist contains correct FRAME-RATE line
  - Timeout/failure: ffmpeg non-zero exit code raises RuntimeError
  - Failure: missing ffmpeg binary raises FileNotFoundError
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── period_renderer ───────────────────────────────────────────────────────────


class TestPeriodRendererLabelValue:
    def test_extracts_formation_label(self) -> None:
        from renderer.period_renderer import _label_value

        labels = [
            {"label_type": "offensive_formation", "label_value": {"formation": "Shotgun"}},
            {"label_type": "play_direction", "label_value": {"direction": "right"}},
        ]
        assert _label_value(labels, "offensive_formation", "formation", "—") == "Shotgun"

    def test_returns_default_when_missing(self) -> None:
        from renderer.period_renderer import _label_value

        labels: list[dict[str, Any]] = []
        assert _label_value(labels, "offensive_formation", "formation", "—") == "—"

    def test_returns_default_for_wrong_key(self) -> None:
        from renderer.period_renderer import _label_value

        labels = [{"label_type": "offensive_formation", "label_value": {}}]
        assert _label_value(labels, "offensive_formation", "formation", "N/A") == "N/A"


class TestPeriodRendererRun:
    def _make_mock_cap(
        self, width: int = 1920, height: int = 1080, frames: int = 3
    ) -> MagicMock:
        cap = MagicMock()
        cap.isOpened.return_value = True
        cap.get.side_effect = lambda prop: {
            3: float(width),   # CAP_PROP_FRAME_WIDTH
            4: float(height),  # CAP_PROP_FRAME_HEIGHT
        }.get(prop, 0.0)
        import numpy as np
        blank = np.zeros((height, width, 3), dtype="uint8")
        side_effects = [(True, blank)] * frames + [(False, blank)]
        cap.read.side_effect = side_effects
        return cap

    def test_happy_path_returns_period_overlay_uri(self) -> None:
        """run() returns dict with period_overlay_uri from R2 upload."""
        from renderer.period_renderer import run

        with (
            patch("cv2.VideoCapture", return_value=self._make_mock_cap()),
            patch("cv2.VideoWriter") as mock_writer_cls,
            patch("cv2.rectangle"),
            patch("cv2.putText"),
            patch("cv2.resize", side_effect=lambda f, *a, **kw: f),
            patch("pipeline.r2.upload_file", return_value="r2://football-iq/period_overlays/c1/period_overlay.mp4"),
        ):
            mock_writer = MagicMock()
            mock_writer_cls.return_value = mock_writer

            result = run(
                clip_id="c1",
                video_path=Path("/fake/clip.mp4"),
                tracklets=[],
                labels=[],
                analytics_safe=True,
                fps=30.0,
            )

        assert result["period_overlay_uri"] == "r2://football-iq/period_overlays/c1/period_overlay.mp4"

    def test_r2_upload_called_with_correct_key(self) -> None:
        """R2 is called with the expected key path."""
        from renderer.period_renderer import run

        with (
            patch("cv2.VideoCapture", return_value=self._make_mock_cap()),
            patch("cv2.VideoWriter"),
            patch("cv2.rectangle"),
            patch("cv2.putText"),
            patch("cv2.resize", side_effect=lambda f, *a, **kw: f),
            patch("pipeline.r2.upload_file", return_value="r2://x/y") as mock_upload,
        ):
            run(
                clip_id="clip-abc",
                video_path=Path("/fake/clip.mp4"),
                tracklets=[],
                labels=[],
                analytics_safe=True,
                fps=25.0,
            )

        r2_key = mock_upload.call_args.args[1]
        assert r2_key == "period_overlays/clip-abc/period_overlay.mp4"

    def test_analytics_unsafe_draws_warning(self) -> None:
        """analytics_safe=False triggers a cv2.rectangle call for the banner."""
        from renderer.period_renderer import run

        with (
            patch("cv2.VideoCapture", return_value=self._make_mock_cap(frames=1)),
            patch("cv2.VideoWriter"),
            patch("cv2.rectangle") as mock_rect,
            patch("cv2.putText"),
            patch("cv2.resize", side_effect=lambda f, *a, **kw: f),
            patch("pipeline.r2.upload_file", return_value="r2://x"),
        ):
            run(
                clip_id="c2",
                video_path=Path("/fake/clip.mp4"),
                tracklets=[],
                labels=[],
                analytics_safe=False,
                fps=30.0,
            )

        # Should have at least one rectangle call for the warning banner
        assert mock_rect.call_count >= 1

    def test_video_capture_open_failure_raises_value_error(self) -> None:
        from renderer.period_renderer import run

        cap = MagicMock()
        cap.isOpened.return_value = False

        with (
            patch("cv2.VideoCapture", return_value=cap),
            pytest.raises(ValueError, match="Unable to open source video"),
        ):
            run(
                clip_id="c-open-fail",
                video_path=Path("/fake/missing.mp4"),
                tracklets=[],
                labels=[],
                analytics_safe=True,
                fps=30.0,
            )

    def test_video_writer_open_failure_raises_value_error(self) -> None:
        from renderer.period_renderer import run

        writer = MagicMock()
        writer.isOpened.return_value = False

        with (
            patch("cv2.VideoCapture", return_value=self._make_mock_cap(frames=1)),
            patch("cv2.VideoWriter", return_value=writer),
            pytest.raises(ValueError, match="Unable to create output video"),
        ):
            run(
                clip_id="c-writer-fail",
                video_path=Path("/fake/clip.mp4"),
                tracklets=[],
                labels=[],
                analytics_safe=True,
                fps=30.0,
            )


# ── hls_encoder ───────────────────────────────────────────────────────────────


class TestHlsEncoder:
    def _fake_ffmpeg_ok(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
        # Create fake TS segments and playlist in the expected directory
        playlist_path = Path(cmd[-1])  # last arg is the playlist path
        seg_dir = playlist_path.parent
        seg_dir.mkdir(parents=True, exist_ok=True)
        # Create two fake segment files
        (seg_dir / "seg_000.ts").write_bytes(b"TS0")
        (seg_dir / "seg_001.ts").write_bytes(b"TS1")
        playlist_path.write_text("#EXTM3U\n#EXT-X-ENDLIST\n")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    def test_happy_path_returns_uris(self) -> None:
        """run() returns master_playlist_uri, media_playlist_uri, segment_uris."""
        from renderer.hls_encoder import run

        upload_calls: list[str] = []

        def fake_upload(path: Path, key: str, **_kwargs: Any) -> str:
            upload_calls.append(key)
            return f"r2://football-iq/{key}"

        with (
            patch("subprocess.run", side_effect=self._fake_ffmpeg_ok),
            patch("pipeline.r2.upload_file", side_effect=fake_upload),
        ):
            result = run(
                clip_id="clip-hls-1",
                overlay_path=Path("/fake/overlay.mp4"),
                fps=30.0,
            )

        assert "master_playlist_uri" in result
        assert "media_playlist_uri" in result
        assert len(result["segment_uris"]) == 2

    def test_master_playlist_content(self) -> None:
        """Master playlist contains FRAME-RATE line."""
        from renderer.hls_encoder import _write_and_upload_master

        uploaded_content: list[str] = []

        def fake_upload(path: Path, key: str, **_kwargs: Any) -> str:
            uploaded_content.append(path.read_text())
            return f"r2://x/{key}"

        with patch("pipeline.r2.upload_file", side_effect=fake_upload):
            _write_and_upload_master("clip-hls-2", 29.97)

        assert any("FRAME-RATE=29.970" in c for c in uploaded_content)

    def test_ffmpeg_failure_raises_runtime_error(self) -> None:
        """Non-zero ffmpeg exit code raises RuntimeError."""
        from renderer.hls_encoder import run

        def fake_ffmpeg_fail(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="ffmpeg: error")

        with (
            patch("subprocess.run", side_effect=fake_ffmpeg_fail),
            patch("pipeline.r2.upload_file"),
        ):
            with pytest.raises(RuntimeError, match="ffmpeg"):
                run(
                    clip_id="clip-hls-3",
                    overlay_path=Path("/fake/overlay.mp4"),
                )

    def test_missing_ffmpeg_raises_file_not_found(self) -> None:
        """FileNotFoundError when ffmpeg is not installed."""
        from renderer.hls_encoder import run

        with (
            patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg not found")),
            patch("pipeline.r2.upload_file"),
        ):
            with pytest.raises(FileNotFoundError):
                run(
                    clip_id="clip-hls-4",
                    overlay_path=Path("/fake/overlay.mp4"),
                )
