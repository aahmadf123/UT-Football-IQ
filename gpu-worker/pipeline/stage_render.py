"""Stage 10 — Overlay Rendering & Dashboard Indexing.

Renders an annotated video overlay on top of each clip:
  - Player bounding boxes with track IDs
  - Coloured motion trails (recent track history per player)
  - Formation / front / play-direction HUD line with a direction arrow
  - Per-player metric callouts (separation, cushion, speed) — only when
    ``analytics_safe`` (suppressed metrics are never drawn)
  - Calibration warning banner when ``analytics_safe=False``

(An on-field yard-line grid deliberately isn't drawn: it needs a trusted
homography, and the interactive clip-review overlay already renders the
field wireframe layer client-side when calibration allows.)

The annotated export is separate from the clean playback proxy so live overlay
toggles continue to work in the film room.

Output: overlay video URI + dashboard index update.
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
from pipeline.runtime import PipelineInterrupted, check_control
from pipeline.video_timeline import VideoTimeline, iter_frames

log = structlog.get_logger(__name__)

FONT = cv2.FONT_HERSHEY_SIMPLEX
TRACK_COLORS = [
    (0, 255, 0),    # green
    (255, 165, 0),  # orange
    (0, 128, 255),  # blue
    (255, 0, 128),  # pink
    (255, 255, 0),  # yellow
]

#: How many frames of history each motion trail shows (~0.5 s at 30 fps).
TRAIL_FRAMES = 15

#: Metric names surfaced as per-player callouts, in preference order.
_CALLOUT_METRICS = ("separation", "cushion", "speed", "max_speed")


def _metric_callouts(
    tracklets: list[dict[str, Any]], metrics: list[dict[str, Any]]
) -> dict[int, str]:
    """Map tracklet *index* → short callout text from coach-facing metrics.

    Only called when ``analytics_safe`` — suppressed metrics never render.
    """
    index_by_id = {str(t.get("id")): i for i, t in enumerate(tracklets)}
    callouts: dict[int, str] = {}
    for name in _CALLOUT_METRICS:
        for m in metrics:
            value = m.get("metric_value") or {}
            provenance = value.get("provenance", {})
            if (m.get("metric_name") != name or m.get("is_suppressed") or m.get("experimental_flag")
                    or (isinstance(provenance, dict) and provenance.get("validated") is False)):
                continue
            idx = index_by_id.get(str(m.get("tracklet_id")))
            if idx is None or idx in callouts:
                continue
            scalar = value.get("value", value.get("yards", value.get("yds_per_s")))
            if isinstance(scalar, (int, float)):
                unit = m.get("unit") or ""
                callouts[idx] = f"{name} {scalar:.1f}{unit[:3]}"
    return callouts


def _draw_direction_arrow(frame: Any, direction: str, w: int, h: int) -> None:
    """Small play-direction arrow next to the HUD line."""
    d = (direction or "").lower()
    if d not in ("left", "right"):
        return
    y = h - 18
    x0, x1 = (w - 60, w - 20) if d == "right" else (w - 20, w - 60)
    cv2.arrowedLine(frame, (x0, y), (x1, y), (255, 255, 255), 2, tipLength=0.35)


def run(
    clip_id: str,
    video_path: Path,
    tracklets: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    analytics_safe: bool,
    fps: float,
    backend_api_url: str,
    *,
    frame_start: int = 0,
    frame_end: int | None = None,
    timeline: VideoTimeline | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> dict[str, Any]:
    """Render overlay video and upload to the object store."""
    log.info("stage_render_start", clip_id=clip_id)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "overlay.mp4"
        mapping = _render(video_path, out_path, tracklets, labels, metrics, analytics_safe, fps,
                          frame_start=frame_start, frame_end=frame_end, timeline=timeline,
                          start_seconds=start_seconds, end_seconds=end_seconds)

        object_key = f"overlays/{clip_id}/overlay.mp4"
        overlay_uri = object_store.upload_file(out_path, object_key, content_type="video/mp4")

    log.info("stage_render_done", clip_id=clip_id, overlay_uri=overlay_uri)
    return {"overlay_uri": overlay_uri, "proxy_timeline": mapping}


def _render(
    video_path: Path,
    out_path: Path,
    tracklets: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
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

    cap = nvdec_video_capture(video_path)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError("Source video could not be opened for rendering")
    if frame_start < 0 or (frame_end is not None and frame_end <= frame_start):
        cap.release()
        raise ValueError("Render frame window must be nonempty and nonnegative")
    timeline = timeline or VideoTimeline.from_path(
        video_path, fps_fallback=fps,
        frame_count_fallback=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
    )
    mapping = None
    copies = None
    if timeline.verified and timeline.timestamps:
        final_frame = frame_end if frame_end is not None else len(timeline.timestamps)
        mapping = timeline.proxy_mapping(frame_start, final_frame, fps,
                                         start_seconds=start_seconds, end_seconds=end_seconds)
        copies = Counter(mapping["proxy_to_source_frame"])
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    # OpenCV's "mp4v" produces MPEG-4 Part 2, which most browsers refuse to
    # play. Write to a temp file and then transcode to H.264 with ffmpeg.
    raw_path = out_path.with_suffix(".raw" + out_path.suffix)
    writer = cv2.VideoWriter(str(raw_path), fourcc, fps, (w, h))
    if not writer.isOpened():
        cap.release()
        writer.release()
        raise RuntimeError("Overlay writer could not be opened")

    # Pre-build track lookup: frame → list of (bbox, color, track_id)
    frame_tracks: dict[int, list[tuple[list[float], tuple[int, int, int], int]]] = {}
    # Per-track ordered box centers for motion trails: index → [(frame, cx, cy)]
    track_centers: list[list[tuple[int, int, int]]] = []
    for i, t in enumerate(tracklets):
        color = TRACK_COLORS[i % len(TRACK_COLORS)]
        centers: list[tuple[int, int, int]] = []
        for pt in t.get("track_points", []):
            fn = pt.get("frame_number", 0)
            bbox = pt.get("bbox")
            if bbox:
                frame_tracks.setdefault(fn, []).append((bbox, color, i))
                cx = int((bbox[0] + bbox[2]) / 2)
                cy = int((bbox[1] + bbox[3]) / 2)
                centers.append((int(fn), cx, cy))
        centers.sort(key=lambda c: c[0])
        track_centers.append(centers)

    # Per-track metric callout (shown only when analytics_safe): the first
    # coach-facing scalar found among separation / cushion / speed.
    callouts = _metric_callouts(tracklets, metrics) if analytics_safe else {}

    # Pre-compute label text
    formation = _label_value(labels, "offensive_formation", "formation", "—")
    front = _label_value(labels, "defensive_front", "front", "—")
    direction = _label_value(labels, "play_direction", "direction", "—")

    try:
        # Frames by timestamp, never by counting (see video_timeline); an
        # undecodable frame is drawn on the next good picture.
        for frame_idx, source in iter_frames(cap, timeline, frame_start, frame_end):
            check_control()
            frame = source.copy()

            # ── Analytics-safe warning ────────────────────────────────────────
            if not analytics_safe:
                cv2.rectangle(frame, (0, 0), (w, 28), (0, 0, 200), -1)
                cv2.putText(frame, "CALIBRATION WARNING — metrics suppressed",
                            (10, 20), FONT, 0.55, (255, 255, 255), 1)

            # ── Formation HUD + play-direction arrow ──────────────────────────
            hud = f"OFF: {formation}  DEF: {front}  DIR: {direction}"
            cv2.putText(frame, hud, (10, h - 12), FONT, 0.5, (255, 255, 255), 1)
            _draw_direction_arrow(frame, direction, w, h)

            # ── Motion trails (recent history per visible track) ──────────────
            for centers in track_centers:
                trail = [(cx, cy) for fn, cx, cy in centers
                         if frame_idx - TRAIL_FRAMES <= fn <= frame_idx]
                for a, b in zip(trail, trail[1:]):
                    cv2.line(frame, a, b, (255, 255, 255), 1, cv2.LINE_AA)

            # ── Player boxes + metric callouts ────────────────────────────────
            for bbox, color, tid in frame_tracks.get(frame_idx, []):
                x1, y1, x2, y2 = [int(v) for v in bbox]
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, f"T{tid}", (x1, y1 - 4), FONT, 0.4, color, 1)
                callout = callouts.get(tid)
                if callout:
                    cv2.putText(frame, callout, (x1, y2 + 14), FONT, 0.4, (255, 255, 255), 1)

            for _ in range(copies.get(frame_idx, 0) if copies is not None else 1):
                check_control()
                writer.write(frame)

    finally:
        cap.release()
        writer.release()

    _transcode_to_h264(raw_path, out_path)
    raw_path.unlink(missing_ok=True)
    return mapping


def _transcode_to_h264(input_path: Path, output_path: Path) -> None:
    """Re-encode a video file to browser-playable H.264 / yuv420p."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "fast",
        "-threads",
        str(max(1, min(4, int(os.environ.get("PIPELINE_CPU_THREADS", "4"))))),
        "-movflags",
        "+faststart",
        "-an",
        str(output_path),
    ]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required to produce a browser-playable overlay") from exc
    try:
        while True:
            check_control()
            try:
                _, error = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError(f"H.264 overlay encoding failed: {error[-500:]}")
    except PipelineInterrupted:
        process.kill()
        process.communicate()
        raise


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


def _update_clip_overlay(clip_id: str, overlay_uri: str, backend_api_url: str) -> None:
    # Route through pipeline.backend so the worker's service-account token is
    # attached — a bare httpx client here 401'd silently and clips never
    # received their overlay URIs.
    if not backend_api_url:
        return
    from pipeline import backend

    backend.patch_clip_storage_uri(clip_id, overlay_uri)
