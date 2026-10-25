"""Prepare source-bound, explicitly unreviewed local detection proposals.

This is an offline staff-review handoff, not pipeline completion, player
identification, or an accuracy benchmark. Originals and reviewed labels are
never modified. Model selection uses the normal router and cached weights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import cv2

from pipeline.detection.official_suppressor import OfficialSuppressor
from pipeline.detector_models import get_detector
from pipeline.media_identity import content_identity
from pipeline.model_router import select_model
from pipeline.stage_detect import _detect
from pipeline.video_timeline import VideoTimeline


def proposal_document(
    source: dict[str, Any], identity: dict[str, Any], timeline: VideoTimeline,
    detections: dict[str, list[dict[str, Any]]], *, model: str, artifact_sha256: str,
) -> dict[str, Any]:
    if not timeline.verified or not timeline.timestamps:
        raise ValueError("Detection review requires verified native source timestamps")
    if source.get("media_sha256") != identity["source_sha256"]:
        raise ValueError("Source bytes do not match the reviewed catalog fingerprint")
    frames = []
    for frame_key, items in sorted(detections.items(), key=lambda entry: int(entry[0])):
        frame = int(frame_key)
        if not 0 <= frame < len(timeline.timestamps):
            raise ValueError("A proposal must refer to a displayed native frame")
        proposals = []
        for index, item in enumerate(items):
            if item.get("class") not in {"player", "official"}:
                continue
            proposals.append({
                "proposal_id": f"frame-{frame}-detection-{index}",
                "bbox": item["bbox"],
                "actor_kind": "referee" if item.get("class") == "official" else "player",
                "confidence": item.get("confidence"),
            })
        frames.append({"timestamp_seconds": timeline.timestamp(frame), "detections": proposals})
    return {
        "schema_version": 1, "evidence_kind": "detection_proposals",
        "source_id": source["id"], "media_sha256": identity["source_sha256"],
        "width": source["width"], "height": source["height"],
        "provenance": {
            "source": "experimental", "model": model, "artifact_sha256": artifact_sha256,
            "official_method": "unvalidated_stripe_heuristic",
            "review_status": "unreviewed", "native_frame_count": len(timeline.timestamps),
            "limitations": ["person boxes may include staff and spectators",
                            "no player identities or offense/defense unit inferred",
                            "not a full pipeline completion or benchmark"],
        },
        "frames": frames,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--weights", required=True, type=Path)
    parser.add_argument("--library", default="practice", choices=("practice", "game", "all"))
    args = parser.parse_args()
    if not args.weights.is_file():
        parser.error("Provide existing cached detector weights; this tool does not download weights")
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    sources = [s for s in catalog["sources"]
               if args.library == "all" or s.get("library") == args.library]
    ids = [s.get("id") for s in sources]
    if any(not isinstance(sid, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", sid) is None
           or sid.casefold() == "inventory" for sid in ids) or len({sid.casefold() for sid in ids}) != len(ids):
        parser.error("Catalog source IDs must be unique safe filenames")
    args.output.mkdir(parents=True, exist_ok=False)
    variant = select_model("detect", 10)
    detector = get_detector(variant, model_path=str(args.weights.resolve()))
    with args.weights.open("rb") as weights_file:
        artifact_hash = hashlib.file_digest(weights_file, "sha256").hexdigest()
    manifest: dict[str, Any] = {"evidence_kind": "local_detection_review_inventory", "rows": [],
                                "production_uploads": 0, "reviewed_labels": 0}
    manifest["producer_source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest["detector_artifact_sha256"] = artifact_hash
    for source in sources:
        path = Path(source["path"])
        before = content_identity(path)
        timeline = VideoTimeline.from_path(path)
        capture = cv2.VideoCapture(str(path))
        try:
            dimensions = (int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
                          int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        finally:
            capture.release()
        if dimensions != (source["width"], source["height"]):
            raise ValueError(f"Catalog dimensions changed: {source['id']}")
        detected = _detect(path, detector, None, OfficialSuppressor(), timeline=timeline)
        expected = len(range(0, len(timeline.timestamps), detected["detection_frame_step"]))
        if detected["inference_frame_count"] != expected:
            raise ValueError(f"Incomplete native decode/inference coverage: {source['id']}")
        after = content_identity(path)
        if before != after:
            raise ValueError(f"Original changed while detecting: {source['id']}")
        document = proposal_document(source, before, timeline, detected["detections"],
                                     model=args.weights.stem, artifact_sha256=artifact_hash)
        document["provenance"]["routed_variant"] = variant
        destination = args.output / f"{source['id']}.json"
        with destination.open("x", encoding="utf-8") as proposal_file:
            json.dump(document, proposal_file, separators=(",", ":"))
        row = {"source_id": source["id"], "filename": source["filename"],
               "media_sha256": before["source_sha256"],
               "native_frames": len(timeline.timestamps),
               "inference_frames": detected["inference_frame_count"],
               "detection_frame_step": detected["detection_frame_step"],
               "frames_with_detections": len(document["frames"]),
               "detections": sum(len(f["detections"]) for f in document["frames"]),
               "referee_candidates": sum(d["actor_kind"] == "referee" for f in document["frames"]
                                         for d in f["detections"]),
               "proposal_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
        manifest["rows"].append(row)
        (args.output / "inventory.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(row), flush=True)
    print(f"Prepared {len(sources)} unreviewed recording proposal files. No production uploads.", flush=True)


if __name__ == "__main__":
    # Explicit fixture/test settings may choose CPU or sampling. Never alter
    # another process's environment, private config, model registry or worker.
    os.environ.setdefault("YOLO_OFFLINE", "true")
    main()
