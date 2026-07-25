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
import tempfile
from pathlib import Path
from typing import Any

import cv2
import structlog

from pipeline import object_store

log = structlog.get_logger(__name__)

FONT = cv2.FONT_HERSHEY_SIMPLEX
TRACK_COLORS = [
    (0, 255, 0),    # green
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
        _render_reduced(video_path, out_path, tracklets, labels, analytics_safe, fps)

        object_key = f"period_overlays/{clip_id}/period_overlay.mp4"
        overlay_uri = object_store.upload_file(out_path, object_key, content_type="video/mp4")

    log.info("period_render_done", clip_id=clip_id, overlay_uri=overlay_uri)
    return {"period_overlay_uri": overlay_uri}


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render_reduced(
    video_path: Path,
    out_path: Path,
    tracklets: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
) -> None:
    from pipeline.hwaccel import nvdec_video_capture

    cap = nvdec_video_capture(video_path)
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"Unable to open source video for period render: {video_path}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Scale to target height, preserve aspect ratio
    scale = PERIOD_RENDER_HEIGHT / src_h if src_h > 0 else 1.0
    out_w = max(1, int(src_w * scale))
    out_h = PERIOD_RENDER_HEIGHT

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (out_w, out_h))
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

    formation = _label_value(labels, "offensive_formation", "formation", "—")
    front = _label_value(labels, "defensive_front", "front", "—")
    direction = _label_value(labels, "play_direction", "direction", "—")

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Downscale frame
        frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

        if not analytics_safe:
            cv2.rectangle(frame, (0, 0), (out_w, 22), (0, 0, 200), -1)
            cv2.putText(frame, "CALIBRATION WARNING",
                        (6, 16), FONT, 0.45, (255, 255, 255), 1)

        cv2.putText(
            frame,
            f"OFF: {formation}  DEF: {front}  DIR: {direction}",
            (6, out_h - 8),
            FONT, 0.38, (255, 255, 255), 1,
        )

        for bbox, color, tid in frame_tracks.get(frame_idx, []):
            x1, y1, x2, y2 = [int(v) for v in bbox]
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 1)
            cv2.putText(frame, f"T{tid}", (x1, max(0, y1 - 3)), FONT, 0.3, color, 1)

        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()


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
