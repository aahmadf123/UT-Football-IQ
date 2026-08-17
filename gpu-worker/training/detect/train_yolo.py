"""Fine-tune the player/official/ball detector on a Roboflow dataset export.

Local-training path (deployable weights). Dataset comes from
``python -m roboflow_ops.download --version N`` (YOLO format). Heavy deps
(``ultralytics``) import lazily per this repo's CI contract.

The dataset's class names must be a subset of the locked taxonomy
(``roboflow_ops.taxonomy.CANONICAL_CLASSES``) — the pipeline resolves
detections by class NAME, so name fidelity matters and order doesn't.

Usage:
    python -m training.detect.train_yolo --data <dataset>/data.yaml \
        [--base yolov8m.pt] [--epochs 60] [--imgsz 1280]

Train at high imgsz: inference runs SAHI tiles (400 px player tiles) over
native resolution, so the model must be comfortable with large inputs and
small players.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from roboflow_ops.taxonomy import CANONICAL_CLASSES


def validate_data_yaml(data_yaml: Path) -> list[str]:
    """Return the dataset's class names, asserting they fit the taxonomy."""
    import yaml

    data = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    raw_names = data.get("names", [])
    names = list(raw_names.values()) if isinstance(raw_names, dict) else list(raw_names)
    unknown = [n for n in names if n not in CANONICAL_CLASSES]
    if unknown:
        raise SystemExit(
            f"data.yaml contains classes outside the locked taxonomy: {unknown}. "
            f"Allowed: {list(CANONICAL_CLASSES)}. Re-generate the Roboflow version "
            "with Modify Classes remapping before training."
        )
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Path to data.yaml")
    parser.add_argument("--base", default="yolov8m.pt", help="Base weights to fine-tune")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--batch", type=int, default=-1, help="-1 = auto batch")
    parser.add_argument("--name", default="footiq-detect", help="Run name (runs/<name>)")
    args = parser.parse_args()

    names = validate_data_yaml(args.data)
    print(f"Training on classes: {names}")

    from ultralytics import YOLO

    model = YOLO(args.base)
    results = model.train(
        data=str(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        name=args.name,
        # Small-object-friendly settings for tiled inference downstream.
        mosaic=1.0,
        scale=0.5,
        degrees=5.0,
    )
    best = Path(results.save_dir) / "weights" / "best.pt"
    print(f"\nBest weights: {best}")
    print(
        "Next: evaluate against the drone-footage baseline, then register:\n"
        f"  python -m training.detect.register --weights {best} --model-name detect-yolov8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
