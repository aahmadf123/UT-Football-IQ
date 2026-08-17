"""Register trained detector weights: upload to R2 artifacts + backend registry.

Closes the loop the serving side already implements: weights go to the
artifacts bucket via the shared storage facade, then a model-version row is
registered through POST /api/v1/mlops/models (experimental by default).
Promotion stays a human decision via the existing mlops promote endpoint;
once promoted, ``pipeline.model_registry_client.resolve("detect")`` serves
the new weights automatically — no router changes.

Usage:
    python -m training.detect.register --weights runs/footiq-detect/weights/best.pt \
        --model-name detect-yolov8 --version 2026-08-17a \
        [--stage detect|ball] [--metrics metrics.json]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--model-name", required=True, help="e.g. detect-yolov8, ball-yolov8n")
    parser.add_argument("--version", default=None, help="Version label (default: timestamp)")
    parser.add_argument("--stage", default="detect", choices=["detect", "ball"])
    parser.add_argument("--metrics", type=Path, default=None, help="JSON file of eval metrics")
    args = parser.parse_args()

    if not args.weights.exists():
        raise SystemExit(f"Weights not found: {args.weights}")

    version = args.version or time.strftime("%Y%m%d-%H%M%S")
    metrics = None
    if args.metrics is not None:
        metrics = json.loads(args.metrics.read_text(encoding="utf-8"))

    from pipeline import backend, storage

    key = f"models/{args.model_name}/{version}/best.pt"
    uri = storage.upload_file(
        args.weights, key, content_type="application/octet-stream", bucket="artifacts"
    )
    print(f"Uploaded weights: {uri}")

    record = backend.register_model_version(
        model_name=args.model_name,
        version=version,
        model_type=args.stage,
        artifact_uri=uri,
        metrics=metrics,
    )
    if record is None:
        print(
            "Backend registration skipped/failed (see logs). The weights are in "
            "storage; re-run registration when the backend is reachable."
        )
        return 1
    print(f"Registered model version {record.get('id')} (experimental).")
    print("Promote after eval: POST /api/v1/mlops/models/{id}/promote")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
