"""Stage 1 — Video Ingestion.

Responsibilities:
  - Download the source video from R2 to a local temp file.
  - Probe FPS, resolution, codec, duration, and detect corruption via ffprobe.
  - Reject or warn on: resolution < 480p, fps < 10, missing metadata,
    unsupported codec (not h264/hevc/vp9/av1).
  - Generate a thumbnail contact sheet (one frame per ~10 s) and upload to R2.
  - Patch the video record with probed metadata.
  - Queue a segment job by updating job output_artifacts.

Output:
  - Video record updated (status=processing, fps, width, height, codec, duration).
  - Thumbnail contact sheet uploaded to R2.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import structlog

from pipeline import backend, r2
from pipeline.homography.regime_detector import (
    UNKNOWN as REGIME_UNKNOWN,
)
from pipeline.homography.regime_detector import (
    CaptureRegimeDetector,
)

log = structlog.get_logger(__name__)

MIN_HEIGHT = 480
MIN_FPS = 10.0
SUPPORTED_CODECS = {"h264", "hevc", "vp9", "av1"}


def run(video_id: str, input_uri: str, job_id: str) -> dict[str, Any]:
    """Run Stage 1 for the given video and return output artifacts."""
    log.info("stage_ingest_start", video_id=video_id)

    # ── Download ──────────────────────────────────────────────────────────
    r2_key = _uri_to_r2_key(input_uri)
    video_path = r2.download_to_temp(r2_key)
    try:
        return _process(video_id, video_path, job_id, input_uri)
    finally:
        video_path.unlink(missing_ok=True)


def _uri_to_r2_key(uri: str) -> str:
    """Pass storage references through — pipeline.storage parses scheme + bucket."""
    return uri


def _process(
    video_id: str, video_path: Path, job_id: str, input_uri: str
) -> dict[str, Any]:
    probe = _ffprobe(video_path)
    warnings: list[str] = []

    fps = probe.get("fps", 0.0)
    width = probe.get("width", 0)
    height = probe.get("height", 0)
    codec = probe.get("codec_name", "unknown")
    duration = probe.get("duration", 0.0)

    # ── Validation ────────────────────────────────────────────────────────
    if height < MIN_HEIGHT:
        warnings.append(f"low_resolution:{width}x{height}")
        log.warning("low_resolution", video_id=video_id, height=height)
    if fps < MIN_FPS:
        warnings.append(f"low_fps:{fps:.1f}")
        log.warning("low_fps", video_id=video_id, fps=fps)
    if codec not in SUPPORTED_CODECS:
        warnings.append(f"unsupported_codec:{codec}")
        log.warning("unsupported_codec", video_id=video_id, codec=codec)
    if duration <= 0:
        warnings.append("missing_duration")
        log.warning("missing_duration", video_id=video_id)

    # ── Thumbnail contact sheet ────────────────────────────────────────────
    sheet_uri = _generate_contact_sheet(video_id, video_path, duration)

    # ── Capture-regime detection (Issue #126) ─────────────────────────────
    # Runs once on the source MP4 so every clip created later inherits the
    # regime. Failures fall back to ``unknown`` / 0.0 — never crash the
    # ingest pipeline because regime is a routing signal, not a gate.
    regime, regime_confidence, regime_features = _detect_capture_regime(
        video_id, video_path
    )

    # ── Update video record ───────────────────────────────────────────────
    backend.patch_video_status(
        video_id,
        "processing",
        duration_seconds=duration,
        fps=fps,
        width=width,
        height=height,
        codec=codec,
        capture_regime=regime,
        regime_confidence=regime_confidence,
    )

    artifacts: dict[str, Any] = {
        "probe": probe,
        "warnings": warnings,
        "thumbnail_uri": sheet_uri,
        # Forward regime to the next pipeline stage's input_artifacts so
        # segment / calibrate / detect can branch without re-querying.
        "capture_regime": regime,
        "regime_confidence": regime_confidence,
        "regime_features": regime_features,
    }
    log.info(
        "stage_ingest_done",
        video_id=video_id,
        warnings=warnings,
        capture_regime=regime,
        regime_confidence=regime_confidence,
    )
    return artifacts


def _detect_capture_regime(
    video_id: str, video_path: Path
) -> tuple[str, float, dict[str, float]]:
    """Run the capture-regime detector with a hard ``unknown`` fallback."""
    try:
        detector = CaptureRegimeDetector()
        result = detector.detect(video_path)
        log.info(
            "regime_detected",
            video_id=video_id,
            regime=result.regime,
            confidence=result.confidence,
            reason_codes=result.reason_codes,
        )
        return result.regime, result.confidence, result.features
    except Exception as exc:
        log.warning("regime_detection_failed", video_id=video_id, error=str(exc))
        return REGIME_UNKNOWN, 0.0, {}


def _ffprobe(path: Path) -> dict[str, Any]:
    """Run ffprobe on the video and return a dict of key metadata."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=60)
        data: dict[str, Any] = json.loads(out)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        log.error("ffprobe_failed", path=str(path), error=str(exc))
        return {}

    result: dict[str, Any] = {}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            result["codec_name"] = stream.get("codec_name", "unknown")
            result["width"] = int(stream.get("width", 0))
            result["height"] = int(stream.get("height", 0))
            # avg_frame_rate is "num/den"
            afr = stream.get("avg_frame_rate", "0/1")
            try:
                num, den = afr.split("/")
                result["fps"] = float(num) / float(den) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                result["fps"] = 0.0
            break
    fmt = data.get("format", {})
    try:
        result["duration"] = float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        result["duration"] = 0.0
    return result


def _generate_contact_sheet(video_id: str, path: Path, duration: float) -> str:
    """Extract ~10 thumbnails spread across the video, tile them, upload to R2."""
    if duration <= 0:
        duration = 60.0  # fallback guess
    interval = max(10.0, duration / 10)
    with tempfile.TemporaryDirectory() as tmp_dir:
        frames_dir = Path(tmp_dir) / "frames"
        frames_dir.mkdir()
        cmd = [
            "ffmpeg",
            "-y",
            "-hwaccel",
            "cuda",
            "-i",
            str(path),
            "-vf",
            f"fps=1/{interval:.1f},scale=320:-1",
            "-frames:v",
            "10",
            str(frames_dir / "thumb%03d.jpg"),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        except Exception:
            # Try without hardware acceleration as fallback
            cmd_fallback = [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vf",
                f"fps=1/{interval:.1f},scale=320:-1",
                "-frames:v",
                "10",
                str(frames_dir / "thumb%03d.jpg"),
            ]
            try:
                subprocess.run(
                    cmd_fallback, check=True, capture_output=True, timeout=120
                )
            except Exception as exc:
                log.warning(
                    "thumbnail_generation_failed", video_id=video_id, error=str(exc)
                )
                return ""

        # Tile into a contact sheet using ffmpeg tile filter
        thumbs = sorted(frames_dir.glob("thumb*.jpg"))
        if not thumbs:
            return ""

        sheet_path = Path(tmp_dir) / "contact_sheet.jpg"
        # Build a tile filter for up to 10 images as a proper list (no shell=True)
        n = len(thumbs)
        cols = min(n, 5)
        rows = (n + cols - 1) // cols
        tile_cmd: list[str] = ["ffmpeg", "-y"]
        for t in thumbs:
            tile_cmd += ["-i", str(t)]
        tile_cmd += ["-filter_complex", f"tile={cols}x{rows}", str(sheet_path)]
        try:
            subprocess.run(tile_cmd, check=True, capture_output=True, timeout=60)
        except Exception as exc:
            log.warning("contact_sheet_tile_failed", video_id=video_id, error=str(exc))
            # Just upload the first thumbnail instead
            if thumbs:
                sheet_path = thumbs[0]
            else:
                return ""

        r2_key = f"thumbnails/{video_id}/contact_sheet.jpg"
        return r2.upload_file(sheet_path, r2_key, content_type="image/jpeg")
