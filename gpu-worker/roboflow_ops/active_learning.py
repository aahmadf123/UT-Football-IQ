"""Active-learning export: send the pipeline's weakest frames to Roboflow.

This is the automated improvement loop behind the zero-touch product: coaches
just upload film; the pipeline records which clips it was least sure about
(calibrated uncertainty → backend active-learning queue); this CLI pulls
those clips, extracts the exact frames, and uploads them tagged
``active-learning`` for auto-label + review. Each round of review makes the
next model better without anyone "doing labeling" as a chore.

Two invariants (review-hardened):
  * Frames come from the RAW parent video, windowed to the clip's
    start/end — never from the rendered clip asset, whose burned-in overlay
    boxes/HUD would leak the previous model's predictions into training data.
  * Successfully exported queue items are advanced to ``in_review`` via
    PATCH /api/v1/mlops/active-learning/queue/{id}, so repeat runs move down
    the queue instead of re-exporting the same top items forever.

Runs on the GPU box (it has backend credentials and storage access). Schedule
it after the nightly pipeline window, or run ad hoc.

Usage:
    python -m roboflow_ops.active_learning [--limit 20] [--frames-per-clip 6]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import structlog

from roboflow_ops.client import get_project, load_config, upload_image
from roboflow_ops.frames import extract_frames

log = structlog.get_logger(__name__)


def fetch_uncertain_clips(limit: int) -> list[dict[str, Any]]:
    """Queued items with the highest calibrated uncertainty from the backend."""
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


def resolve_clip_window(clip_id: str) -> tuple[Any, float, float] | None:
    """(local raw-video path, clip start s, clip end s) for a queued clip.

    Always the RAW parent video — the rendered clip asset carries burned-in
    overlays that must never reach the training set.
    """
    from pipeline import backend, storage

    with backend._client() as c:  # noqa: SLF001
        clip = c.get(f"/api/v1/clips/{clip_id}").json()
        video = c.get(f"/api/v1/videos/{clip['video_id']}").json()
    uri = video.get("storage_uri")
    if not uri:
        return None
    local = storage.download_to_temp(uri)
    return local, float(clip.get("start_time", 0.0)), float(clip.get("end_time", 0.0))


def mark_in_review(item_id: str) -> bool:
    """Advance a queue item so the next run doesn't re-export it."""
    from pipeline import backend

    try:
        with backend._client() as c:  # noqa: SLF001
            resp = c.patch(
                f"/api/v1/mlops/active-learning/queue/{item_id}",
                json={"status": "in_review"},
            )
            resp.raise_for_status()
            return True
    except Exception as exc:
        log.warning("active_learning_status_update_failed", item=item_id, error=str(exc)[:200])
        return False


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
    advanced = 0
    for item in items:
        item_id = str(item.get("id") or "")
        clip_id = str(item.get("clip_id") or "")
        if not clip_id:
            continue
        try:
            resolved = resolve_clip_window(clip_id)
        except Exception as exc:
            log.warning("active_learning_clip_fetch_failed", clip=clip_id, error=str(exc)[:200])
            continue
        if resolved is None:
            continue
        local, start_s, end_s = resolved
        frames = extract_frames(
            Path(local),
            cfg.data_dir / "active-learning" / clip_id,
            args.frames_per_clip,
            start_seconds=start_s,
            end_seconds=end_s if end_s > start_s else None,
        )
        clip_uploaded = 0
        for frame in frames:
            if upload_image(
                project,
                frame,
                batch_name=args.batch,
                tags=["active-learning", f"clip:{clip_id}"],
            ):
                clip_uploaded += 1
        uploaded += clip_uploaded
        if clip_uploaded > 0 and item_id and mark_in_review(item_id):
            advanced += 1

    print(
        f"Uploaded {uploaded} active-learning frames from {len(items)} queued clips; "
        f"{advanced} items advanced to in_review."
    )
    print("Next: python -m roboflow_ops.autolabel --batch active-learning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
