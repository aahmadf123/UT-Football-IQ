"""Unit tests for gpu-worker/renderer/period_renderer.py and hls_encoder.py.

All tests run without GPU hardware, real video files, or object storage.
cv2 and ffmpeg are mocked so the tests are fast and hermetic.

period_renderer covers:
  - Happy path: run() uploads to the object store and returns period_overlay_uri
  - Reviewed staff HUD uses the separate effective annotation snapshot
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


class TestReviewedHud:
    @pytest.mark.parametrize("source", ["synthetic_test", "synthetic", "stub", "heuristic",
                                       "unavailable", "disabled", "unverified_adapter"])
    @pytest.mark.parametrize("nested", [False, True])
    def test_disqualified_source_cannot_be_replayed_as_staff(self, source, nested) -> None:
        from pipeline.reviewed_labels import reviewed_label_data

        evidence = {"source": source}
        value = {"formation": "Pistol", **({"provenance": evidence} if nested else evidence)}
        label = {"source": "human", "annotated_by": "cf128509-d42d-4532-8c4d-67e44150d391",
                 "label_type": "offensive_formation", "label_value": value}
        assert reviewed_label_data([label]) == {}

    def test_only_attributed_unambiguous_human_labels_enter_staff_snapshot(self) -> None:
        from pipeline.reviewed_labels import reviewed_label_data

        actor = "cf128509-d42d-4532-8c4d-67e44150d391"
        human = {"source": "human", "annotated_by": actor,
                 "label_type": "offensive_formation", "label_value": {"formation": "Pistol"}}
        raw = {**human, "source": "model", "validated": True, "qualification": "human_reviewed"}
        assert reviewed_label_data([raw, {**human, "annotated_by": None}]) == {}
        assert reviewed_label_data([raw, human]) == {"offensive_formation": {"formation": "Pistol"}}
        assert reviewed_label_data([human, {**human, "label_value": {"formation": "Shotgun"}}]) == {}
        assert reviewed_label_data([{**human, "tracklet_id": "actor"}]) == {}
        assert reviewed_label_data([{**human, "label_value": {"formation": "Pistol", "source": "heuristic"}}]) == {}

    def test_extracts_formation_label(self) -> None:
        from pipeline.reviewed_labels import reviewed_hud

        labels = {"offensive_formation": {"formation": "Shotgun"},
                  "play_direction": {"direction": "right"}}
        assert reviewed_hud(labels) == ("STAFF  OFF: Shotgun  DIR: right", "right")

    def test_returns_default_when_missing(self) -> None:
        from pipeline.reviewed_labels import reviewed_hud

        assert reviewed_hud(None) == ("", "")

    def test_returns_default_for_wrong_key(self) -> None:
        from pipeline.reviewed_labels import reviewed_hud

        labels = {"offensive_formation": {}, "defensive_front": {"front": {"nested": "4-3"}},
                  "play_direction": {"direction": "unknown"}}
        assert reviewed_hud(labels) == ("", "")


class TestPeriodRendererRun:
    def _make_mock_cap(self, width: int = 1920, height: int = 1080, frames: int = 3) -> Any:
        from tests.fake_capture import FakeCapture

        return FakeCapture(frames, width=width, height=height)

    def test_happy_path_returns_period_overlay_uri(self) -> None:
        """run() returns dict with period_overlay_uri from object upload."""
        from renderer.period_renderer import run

        with (
            patch("cv2.VideoCapture", return_value=self._make_mock_cap()),
            patch("cv2.VideoWriter") as mock_writer_cls,
            patch("renderer.period_renderer._transcode_h264"),
            patch("cv2.rectangle"),
            patch("cv2.putText"),
            patch("cv2.resize", side_effect=lambda f, *a, **kw: f),
            patch(
                "pipeline.object_store.upload_file",
                return_value="s3://football-iq/period_overlays/c1/period_overlay.mp4",
            ),
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

        assert (
            result["period_overlay_uri"] == "s3://football-iq/period_overlays/c1/period_overlay.mp4"
        )

    def test_object_upload_called_with_correct_key(self) -> None:
        """the object store is called with the expected key path."""
        from renderer.period_renderer import run

        with (
            patch("cv2.VideoCapture", return_value=self._make_mock_cap()),
            patch("cv2.VideoWriter"),
            patch("renderer.period_renderer._transcode_h264"),
            patch("cv2.rectangle"),
            patch("cv2.putText"),
            patch("cv2.resize", side_effect=lambda f, *a, **kw: f),
            patch("pipeline.object_store.upload_file", return_value="s3://x/y") as mock_upload,
        ):
            run(
                clip_id="clip-abc",
                video_path=Path("/fake/clip.mp4"),
                tracklets=[],
                labels=[],
                analytics_safe=True,
                fps=25.0,
            )

        object_key = mock_upload.call_args.args[1]
        assert object_key.startswith("period_overlays/clip-abc/pts-overlay-v6-native-staff-hud/")
        assert object_key.endswith("/period_overlay.mp4")

    def test_analytics_unsafe_draws_warning(self) -> None:
        """analytics_safe=False triggers a cv2.rectangle call for the banner."""
        from renderer.period_renderer import run

        with (
            patch("cv2.VideoCapture", return_value=self._make_mock_cap(frames=1)),
            patch("cv2.VideoWriter"),
            patch("renderer.period_renderer._transcode_h264"),
            patch("cv2.rectangle") as mock_rect,
            patch("cv2.putText"),
            patch("cv2.resize", side_effect=lambda f, *a, **kw: f),
            patch("pipeline.object_store.upload_file", return_value="s3://x"),
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
            return f"s3://football-iq/{key}"

        with (
            patch("subprocess.run", side_effect=self._fake_ffmpeg_ok),
            patch("pipeline.object_store.upload_file", side_effect=fake_upload),
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
            return f"s3://x/{key}"

        with patch("pipeline.object_store.upload_file", side_effect=fake_upload):
            _write_and_upload_master("clip-hls-2", 29.97)

        assert any("FRAME-RATE=29.970" in c for c in uploaded_content)

    def test_ffmpeg_failure_raises_runtime_error(self) -> None:
        """Non-zero ffmpeg exit code raises RuntimeError."""
        from renderer.hls_encoder import run

        def fake_ffmpeg_fail(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="ffmpeg: error")

        with (
            patch("subprocess.run", side_effect=fake_ffmpeg_fail),
            patch("pipeline.object_store.upload_file"),
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
            patch("pipeline.object_store.upload_file"),
        ):
            with pytest.raises(FileNotFoundError):
                run(
                    clip_id="clip-hls-4",
                    overlay_path=Path("/fake/overlay.mp4"),
                )
