"""Full-resolution HLS encoder for nightly follow-up jobs.

Converts a full-quality overlay video (produced by stage_render.py) into an
HLS (HTTP Live Streaming) package consisting of:
  - A master playlist  ``hls/{clip_id}/master.m3u8``
  - A media playlist   ``hls/{clip_id}/index.m3u8``
  - Segment files      ``hls/{clip_id}/seg_NNN.ts``

All outputs are uploaded to the object store so the frontend can stream via the object store public
endpoint or through the backend's signed-URL proxy.

Encoding relies on the ``ffmpeg`` binary which must be available in PATH inside
the GPU worker container.

Environment variables:
  HLS_SEGMENT_DURATION — target HLS segment length in seconds (default: 4)
  HLS_VIDEO_BITRATE    — video bitrate string passed to ffmpeg (default: 2000k)
  HLS_AUDIO_BITRATE    — audio bitrate string (default: 128k)
  S3_*                 — forwarded to pipeline.object_store upload helpers
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import structlog

from pipeline import object_store

log = structlog.get_logger(__name__)

HLS_SEGMENT_DURATION = int(os.environ.get("HLS_SEGMENT_DURATION", "4"))
HLS_VIDEO_BITRATE = os.environ.get("HLS_VIDEO_BITRATE", "2000k")
HLS_AUDIO_BITRATE = os.environ.get("HLS_AUDIO_BITRATE", "128k")


# ── Public API ────────────────────────────────────────────────────────────────


def run(
    clip_id: str,
    overlay_path: Path,
    fps: float = 30.0,
) -> dict[str, Any]:
    """Encode overlay video to HLS and upload all segments to the object store.

    Args:
        clip_id:      Clip UUID used as the object key prefix.
        overlay_path: Local path to the full-resolution overlay MP4.
        fps:          Source frame rate (informational; ffmpeg probes the file).

    Returns:
        Dict with keys:
          ``master_playlist_uri`` — object storage URI of the HLS master playlist.
          ``media_playlist_uri``  — object storage URI of the media playlist.
          ``segment_uris``        — List of object storage URI for all TS segments.
    """
    log.info("hls_encode_start", clip_id=clip_id, source=str(overlay_path))

    with tempfile.TemporaryDirectory() as tmp_dir:
        hls_dir = Path(tmp_dir) / "hls"
        hls_dir.mkdir()
        playlist_path = hls_dir / "index.m3u8"

        _encode_hls(overlay_path, playlist_path)

        # Upload all generated files
        segment_uris = _upload_segments(hls_dir, clip_id)
        media_uri = _upload_playlist(playlist_path, clip_id, "index.m3u8")
        master_uri = _write_and_upload_master(clip_id, fps)

    log.info(
        "hls_encode_done",
        clip_id=clip_id,
        segments=len(segment_uris),
        master_uri=master_uri,
    )
    return {
        "master_playlist_uri": master_uri,
        "media_playlist_uri": media_uri,
        "segment_uris": segment_uris,
    }


# ── FFmpeg encode ─────────────────────────────────────────────────────────────


def _encode_hls(source: Path, playlist_path: Path) -> None:
    from pipeline.hwaccel import nvenc_ffmpeg_codec_args

    codec_args = nvenc_ffmpeg_codec_args(bitrate=HLS_VIDEO_BITRATE)
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source),
        *codec_args,
        "-c:a", "aac",
        "-b:a", HLS_AUDIO_BITRATE,
        "-profile:v", "baseline",
        "-level", "3.0",
        "-start_number", "0",
        "-hls_time", str(HLS_SEGMENT_DURATION),
        "-hls_list_size", "0",           # keep all segments in the playlist
        "-hls_segment_filename", str(playlist_path.parent / "seg_%03d.ts"),
        "-f", "hls",
        str(playlist_path),
    ]
    log.info("ffmpeg_hls_encode", cmd=" ".join(cmd))
    result = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        log.error("ffmpeg_hls_encode_failed", stderr=result.stderr[-2000:])
        raise RuntimeError(f"ffmpeg HLS encode failed: {result.stderr[-2000:]}")
    log.info("ffmpeg_hls_encode_ok")


# ── Upload helpers ────────────────────────────────────────────────────────────


def _upload_segments(hls_dir: Path, clip_id: str) -> list[str]:
    uris: list[str] = []
    for seg in sorted(hls_dir.glob("seg_*.ts")):
        object_key = f"hls/{clip_id}/{seg.name}"
        uri = object_store.upload_file(seg, object_key, content_type="video/MP2T")
        uris.append(uri)
    return uris


def _upload_playlist(playlist_path: Path, clip_id: str, filename: str) -> str:
    object_key = f"hls/{clip_id}/{filename}"
    return object_store.upload_file(playlist_path, object_key, content_type="application/vnd.apple.mpegurl")


def _write_and_upload_master(clip_id: str, fps: float) -> str:
    """Write a minimal HLS master playlist and upload it to the object store."""
    import tempfile as _tf

    master_content = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-STREAM-INF:BANDWIDTH=2000000,FRAME-RATE={fps:.3f}\n"
        "index.m3u8\n"
    )
    with _tf.NamedTemporaryFile(
        mode="w", suffix=".m3u8", delete=False
    ) as tmp:
        tmp.write(master_content)
        tmp_path = Path(tmp.name)

    try:
        return _upload_playlist(tmp_path, clip_id, "master.m3u8")
    finally:
        tmp_path.unlink(missing_ok=True)
