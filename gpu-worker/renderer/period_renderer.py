"""Period-break lightweight overlay renderer.

Produces a reduced-resolution (540 p) annotated video for period-break
delivery to coaching staff.  Omits full HLS encoding — the output is a
single MP4 file that can be streamed directly from the object store.

Design goals:
  - Fast: target < 90 s render for a 5-min clip on a GTX 1660 Ti.
  - Low storage footprint: 540p output is ~4× smaller than 1080p.
  - Overlays: player bounding boxes, formation HUD, confidence warning.
  - No pose keypoints or metric callouts (those arrive in nightly full run).

The nightly full-resolution render is handled by ``renderer.hls_encoder``.

Environment variables:
  PERIOD_RENDER_HEIGHT — target output height in pixels (default: 540)
  S3_*                 — forwarded to pipeline.object_store upload helpers
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import cv2
import structlog

from pipeline import object_store
from pipeline.runtime import check_control
from pipeline.video_timeline import VideoTimeline, seek_source_frame

log = structlog.get_logger(__name__)

FONT = cv2.FONT_HERSHEY_SIMPLEX
TRACK_COLORS = [
    (0, 255, 0),  # green
    (255, 165, 0),  # orange
    (0, 128, 255),  # blue
    (255, 0, 128),  # pink
    (255, 255, 0),  # yellow
]

PERIOD_RENDER_HEIGHT = int(os.environ.get("PERIOD_RENDER_HEIGHT", "540"))


# ── Public API ────────────────────────────────────────────────────────────────


def run(
    clip_id: str,
    video_path: Path,
    tracklets: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
    *,
    frame_start: int = 0,
    frame_end: int | None = None,
    timeline: VideoTimeline | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> dict[str, Any]:
    """Render a reduced-resolution period-break overlay and upload to the object store.

    Args:
        clip_id:       Clip UUID used as the object key prefix.
        video_path:    Local path to the source clip video.
        tracklets:     List of tracklet dicts (same schema as stage_render).
        labels:        List of label dicts for formation HUD.
        analytics_safe: If False a calibration-warning banner is drawn.
        fps:           Source frame rate.

    Returns:
        Dict with key ``period_overlay_uri`` pointing to the stored object.
    """
    log.info("period_render_start", clip_id=clip_id, target_height=PERIOD_RENDER_HEIGHT)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "period_overlay.mp4"
        proxy_timeline = _render_reduced(
            video_path,
            out_path,
            tracklets,
            labels,
            analytics_safe,
            fps,
            frame_start=frame_start,
            frame_end=frame_end,
            timeline=timeline,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )

        object_key = f"period_overlays/{clip_id}/period_overlay.mp4"
        overlay_uri = object_store.upload_file(out_path, object_key, content_type="video/mp4")

    log.info("period_render_done", clip_id=clip_id, overlay_uri=overlay_uri)
    return {
        "period_overlay_uri": overlay_uri,
        "source_start_frame": frame_start,
        "overlay_start_time_seconds": 0,
        "source_start_time_seconds": (proxy_timeline or {}).get(
            "source_start_seconds",
            start_seconds if start_seconds is not None else frame_start / fps,
        ),
        "proxy_timeline": proxy_timeline,
    }


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render_reduced(
    video_path: Path,
    out_path: Path,
    tracklets: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
    *,
    frame_start: int = 0,
    frame_end: int | None = None,
    timeline: VideoTimeline | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> dict[str, Any] | None:
    from pipeline.hwaccel import nvdec_video_capture

    if frame_start < 0 or (frame_end is not None and frame_end <= frame_start):
        raise ValueError("Render window must have positive duration")
    cap = nvdec_video_capture(video_path)
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"Unable to open source video for period render: {video_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Scale to target height, preserve aspect ratio
    scale = min(1.0, PERIOD_RENDER_HEIGHT / src_h) if src_h > 0 else 1.0
    out_w = max(2, int(src_w * scale) // 2 * 2)
    out_h = max(2, int(src_h * scale) // 2 * 2)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    raw_path = out_path.with_suffix(".raw.mp4")
    writer = cv2.VideoWriter(str(raw_path), fourcc, fps, (out_w, out_h))
    if not writer.isOpened():
        cap.release()
        writer.release()
        raise ValueError(f"Unable to create output video for period render: {out_path}")

    # Pre-build track lookup scaled to output resolution
    frame_tracks: dict[int, list[tuple[list[float], tuple[int, int, int], int]]] = {}
    for i, t in enumerate(tracklets):
        color = TRACK_COLORS[i % len(TRACK_COLORS)]
        for pt in t.get("track_points", []):
            fn = pt.get("frame_number", 0)
            bbox = pt.get("bbox")
            if bbox:
                scaled = [
                    bbox[0] * scale,
                    bbox[1] * scale,
                    bbox[2] * scale,
                    bbox[3] * scale,
                ]
                frame_tracks.setdefault(fn, []).append((scaled, color, i))

    if timeline is None and video_path.is_file():
        timeline = VideoTimeline.from_path(
            video_path,
            fps_fallback=fps,
            frame_count_fallback=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        )
    proxy_timeline = None
    repetitions = None
    if timeline is not None and timeline.timestamps:
        frame_end = min(
            frame_end if frame_end is not None else len(timeline.timestamps),
            len(timeline.timestamps),
        )
        proxy_timeline = timeline.proxy_mapping(
            frame_start,
            frame_end,
            fps,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        repetitions = Counter(proxy_timeline["proxy_to_source_frame"])

    formation = _label_value(labels, "offensive_formation", "formation", "—")
    front = _label_value(labels, "defensive_front", "front", "—")
    direction = _label_value(labels, "play_direction", "direction", "—")

    try:
        if timeline is not None:
            seek_source_frame(cap, frame_start, timeline)
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_start)
        frame_idx = frame_start
        while frame_end is None or frame_idx < frame_end:
            check_control()
            ret, frame = cap.read()
            if not ret:
                break

            # Downscale frame
            frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

            if not analytics_safe:
                cv2.rectangle(frame, (0, 0), (out_w, 22), (0, 0, 200), -1)
                cv2.putText(frame, "CALIBRATION WARNING", (6, 16), FONT, 0.45, (255, 255, 255), 1)

            cv2.putText(
                frame,
                f"OFF: {formation}  DEF: {front}  DIR: {direction}",
                (6, out_h - 8),
                FONT,
                0.38,
                (255, 255, 255),
                1,
            )

            for bbox, color, tid in frame_tracks.get(frame_idx, []):
                x1, y1, x2, y2 = [int(v) for v in bbox]
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 1)
                cv2.putText(frame, f"T{tid}", (x1, max(0, y1 - 3)), FONT, 0.3, color, 1)

            for _ in range(repetitions.get(frame_idx, 0) if repetitions is not None else 1):
                writer.write(frame)
            frame_idx += 1

    finally:
        cap.release()
        writer.release()
    check_control()
    _transcode_h264(raw_path, out_path)
    raw_path.unlink(missing_ok=True)
    return proxy_timeline


def _transcode_h264(source: Path, destination: Path) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "25",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(destination),
    ]
    from worker.media_process import run_checked

    try:
        run_checked(command, timeout=180)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Browser-compatible overlay encoding requires working ffmpeg/libx264"
        ) from exc


def _label_value(
    labels: list[dict[str, Any]],
    label_type: str,
    key: str,
    default: str,
) -> str:
    for lb in labels:
        if lb.get("label_type") == label_type:
            return str(lb.get("label_value", {}).get(key, default))
    return default
