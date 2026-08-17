"""Active-learning export: send the pipeline's weakest frames to Roboflow.

This is the automated improvement loop behind the zero-touch product: coaches
just upload film; the pipeline records which clips it was least sure about
(calibrated uncertainty → backend active-learning queue); this CLI pulls
those clips, extracts the exact frames, and uploads them tagged
``active-learning`` for auto-label + review. Each round of review makes the
next model better without anyone "doing labeling" as a chore.

Runs on the GPU box (it has backend credentials and storage access). Schedule
it after the nightly pipeline window, or run ad hoc.

Usage:
    python -m roboflow_ops.active_learning [--limit 20] [--frames-per-clip 6]
"""

from __future__ import annotations

import argparse
from typing import Any

import structlog

from roboflow_ops.client import get_project, load_config, upload_image
from roboflow_ops.frames import extract_frames

log = structlog.get_logger(__name__)


def fetch_uncertain_clips(limit: int) -> list[dict[str, Any]]:
    """Clips with the highest calibrated uncertainty from the backend queue."""
    import httpx

    from pipeline import backend

    if not backend.BACKEND_API_URL:
        log.warning("active_learning_offline", hint="BACKEND_API_URL is not set")
        return []
    try:
        with backend._client() as c:  # noqa: SLF001 - shared worker client
            resp = c.get("/api/v1/mlops/active-learning/queue", params={"limit": limit})
            resp.raise_for_status()
            data = resp.json()
            items: list[dict[str, Any]] = data if isinstance(data, list) else data.get("items", [])
            return items
    except httpx.HTTPError as exc:
        log.warning("active_learning_fetch_failed", error=str(exc)[:200])
        return []


def resolve_clip_video(clip_id: str) -> Any:
    """Local path for a clip's playable asset (rendered clip or parent video)."""
    from pipeline import backend, storage

    with backend._client() as c:  # noqa: SLF001
        clip = c.get(f"/api/v1/clips/{clip_id}").json()
        uri = clip.get("storage_uri")
        if not uri:
            video = c.get(f"/api/v1/videos/{clip['video_id']}").json()
            uri = video.get("storage_uri")
    if not uri:
        return None
    return storage.download_to_temp(uri)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="Max queue items to process")
    parser.add_argument("--frames-per-clip", type=int, default=6)
    parser.add_argument("--batch", default="active-learning")
    args = parser.parse_args()

    cfg = load_config()
    project = get_project(cfg)
    items = fetch_uncertain_clips(args.limit)
    if not items:
        print("Active-learning queue is empty (or backend unreachable). Nothing to export.")
        return 0

    uploaded = 0
    for item in items:
        clip_id = str(item.get("clip_id") or item.get("id") or "")
        if not clip_id:
            continue
        try:
            local = resolve_clip_video(clip_id)
        except Exception as exc:
            log.warning("active_learning_clip_fetch_failed", clip=clip_id, error=str(exc)[:200])
            continue
        if local is None:
            continue
        frames = extract_frames(
            local, cfg.data_dir / "active-learning" / clip_id, args.frames_per_clip
        )
        for frame in frames:
            if upload_image(
                project,
                frame,
                batch_name=args.batch,
                tags=["active-learning", f"clip:{clip_id}"],
            ):
                uploaded += 1

    print(f"Uploaded {uploaded} active-learning frames from {len(items)} queued clips.")
    print("Next: python -m roboflow_ops.autolabel --batch active-learning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
