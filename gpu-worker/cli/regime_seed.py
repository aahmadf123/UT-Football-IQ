"""CLI: build a 50-clip seed set for capture-regime validation (Issue #126).

Fetches a batch of clips from the backend, runs the
``CaptureRegimeDetector`` over their associated source video, and writes
the predictions to ``data/regime_seed_predictions.csv`` for manual review.

Reviewers copy the corrected rows into ``data/regime_seed_labels.csv``.
A follow-up branch will use that file to fit logistic-regression
coefficients and drop the resulting ``regime_clf.joblib`` into
``gpu-worker/models/``.

Usage::

    python -m cli.regime_seed \\
        --backend-url $BACKEND_API_URL \\
        --token $FOOTBALL_IQ_TOKEN \\
        --limit 50 \\
        --output data/regime_seed_predictions.csv

The CLI is intentionally a thin sidecar — it does not write to the
backend or the database. The full ingest path (which DOES persist
regime metadata on every new video/clip) lives in
``gpu-worker/pipeline/stage_ingest.py``.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any

import httpx


def _fetch_clips(
    *,
    backend_url: str,
    token: str,
    limit: int,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Collect up to ``limit`` clips by paging videos and listing their clips.

    The backend exposes ``GET /api/v1/videos`` and
    ``GET /api/v1/videos/{id}/clips`` but no top-level ``GET /api/v1/clips``
    list endpoint, so we iterate videos (newest first per the router's
    default ordering) and accumulate clips until we hit ``limit``.
    """
    headers = {"Authorization": f"Bearer {token}"}
    clips: list[dict[str, Any]] = []
    with httpx.Client(base_url=backend_url, timeout=timeout) as c:
        video_offset = 0
        video_page = 50
        while len(clips) < limit:
            v_resp = c.get(
                "/api/v1/videos",
                params={"limit": video_page, "offset": video_offset},
                headers=headers,
            )
            v_resp.raise_for_status()
            videos = list(v_resp.json())
            if not videos:
                break
            for video in videos:
                if len(clips) >= limit:
                    break
                video_id = video.get("id")
                if not video_id:
                    continue
                remaining = limit - len(clips)
                c_resp = c.get(
                    f"/api/v1/videos/{video_id}/clips",
                    params={"limit": remaining},
                    headers=headers,
                )
                if c_resp.status_code != 200:
                    continue
                clips.extend(c_resp.json())
            video_offset += len(videos)
    return clips[:limit]


def _video_uri(
    *, backend_url: str, token: str, video_id: str, timeout: float = 30.0
) -> str | None:
    """GET /api/v1/videos/{id} and return its R2 storage URI."""
    with httpx.Client(base_url=backend_url, timeout=timeout) as c:
        resp = c.get(
            f"/api/v1/videos/{video_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("storage_uri")


def _predict_for_clip(clip: dict[str, Any], video_uri: str | None) -> dict[str, Any]:
    """Run the detector on the clip's parent video (best-effort).

    Falls back to ``unknown`` if R2 is unreachable or the detector raises
    — the CLI is for offline review, not a gate.
    """
    from pipeline import r2
    from pipeline.homography.regime_detector import UNKNOWN, CaptureRegimeDetector

    if not video_uri:
        return {"regime": UNKNOWN, "confidence": 0.0, "features": {}}

    r2_key = (
        video_uri[len("r2://") :].split("/", 1)[1]
        if video_uri.startswith("r2://")
        else video_uri
    )
    video_path: Path | None = None
    try:
        video_path = r2.download_to_temp(r2_key)
        detector = CaptureRegimeDetector()
        result = detector.detect(video_path)
        return {
            "regime": result.regime,
            "confidence": result.confidence,
            "features": result.features,
        }
    except Exception as exc:
        print(f"warn: detect failed for {clip.get('id')}: {exc}", file=sys.stderr)
        return {"regime": UNKNOWN, "confidence": 0.0, "features": {}}
    finally:
        if video_path is not None:
            video_path.unlink(missing_ok=True)


def run(
    *,
    backend_url: str,
    token: str,
    limit: int,
    output: Path,
    dry_run: bool = False,
) -> int:
    clips = _fetch_clips(backend_url=backend_url, token=token, limit=limit)
    if not clips:
        print("no clips returned by backend", file=sys.stderr)
        return 1

    rows: list[dict[str, Any]] = []
    for clip in clips:
        clip_id = clip.get("id", "")
        video_id = clip.get("video_id", "")
        if dry_run:
            prediction = {"regime": "unknown", "confidence": 0.0, "features": {}}
        else:
            uri = _video_uri(backend_url=backend_url, token=token, video_id=video_id)
            prediction = _predict_for_clip(clip, uri)
        rows.append(
            {
                "clip_id": clip_id,
                "video_id": video_id,
                "predicted_regime": prediction["regime"],
                "confidence": prediction["confidence"],
                "static_bg_score": prediction["features"].get("static_bg_score", ""),
                "vp_altitude_score": prediction["features"].get(
                    "vp_altitude_score", ""
                ),
                "global_affine_score": prediction["features"].get(
                    "global_affine_score", ""
                ),
                "framing_breakout_score": prediction["features"].get(
                    "framing_breakout_score", ""
                ),
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} predictions → {output}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--backend-url",
        default=os.environ.get("BACKEND_API_URL", ""),
        help="Backend base URL (defaults to $BACKEND_API_URL).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("FOOTBALL_IQ_TOKEN", ""),
        help="Bearer token for the backend (defaults to $FOOTBALL_IQ_TOKEN).",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/regime_seed_predictions.csv"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip detector + R2 fetch; emit zeroed predictions only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.backend_url:
        print("--backend-url (or $BACKEND_API_URL) is required", file=sys.stderr)
        return 2
    if not args.token and not args.dry_run:
        print("--token (or $FOOTBALL_IQ_TOKEN) is required", file=sys.stderr)
        return 2
    return run(
        backend_url=args.backend_url,
        token=args.token,
        limit=args.limit,
        output=args.output,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
