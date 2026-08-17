"""Download a generated dataset version for local training.

Usage:
    python -m roboflow_ops.download --version 3 [--format yolov8]

The dataset lands under ``ROBOFLOW_DATA_DIR/datasets/<project>-<version>``
(gitignored). Point ``training/detect/train_yolo.py --data`` at the
``data.yaml`` inside it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from roboflow_ops.client import get_project, load_config


def download_version(version_number: int, export_format: str = "yolov8") -> Path:
    cfg = load_config()
    project = get_project(cfg)
    target = cfg.data_dir / "datasets" / f"{cfg.project}-{version_number}"
    target.mkdir(parents=True, exist_ok=True)
    dataset = project.version(version_number).download(
        model_format=export_format, location=str(target), overwrite=True
    )
    return Path(dataset.location)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--format", default="yolov8")
    args = parser.parse_args()
    location = download_version(args.version, args.format)
    print(f"Dataset downloaded to: {location}")
    print(f"Train with: python -m training.detect.train_yolo --data {location}/data.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
