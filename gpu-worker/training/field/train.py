"""Train the yard-number detector used for automatic field registration.

Local GPU training (deployable weights, $0). Heavy deps import lazily.

Usage:
    python -m training.field.train --data roboflow-data/field_numbers/data.yaml \
        [--base yolo11s.pt] [--epochs 60] [--imgsz 640] [--batch 16]

The export's ``data.yaml`` uses split paths relative to an unknown root, so
a resolved copy is written next to it before training.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

#: Class names this repo's numeral fit understands (see numeral_fit.CLASS_TO_FIELD).
EXPECTED_CLASSES = {
    "f5", "fl1", "fl2", "fl3", "fl4", "fr1", "fr2", "fr3", "fr4",
    "n5", "nl1", "nl2", "nl3", "nl4", "nr1", "nr2", "nr3", "nr4",
}  # fmt: skip


def resolved_data_yaml(data_yaml: Path) -> Path:
    """Write ``data.resolved.yaml`` with absolute split paths; check classes."""
    import yaml

    data = yaml.safe_load(data_yaml.read_text(encoding="utf-8"))
    names = data.get("names", [])
    names = list(names.values()) if isinstance(names, dict) else list(names)
    unknown = set(names) - EXPECTED_CLASSES
    if unknown:
        raise SystemExit(f"Unexpected classes {sorted(unknown)}; update numeral_fit first.")
    root = data_yaml.parent.resolve()
    resolved = {
        "path": str(root),
        "train": "train/images",
        "val": "valid/images",
        "test": "test/images",
        "names": names,
        "nc": len(names),
    }
    out = data_yaml.with_name("data.resolved.yaml")
    out.write_text(yaml.safe_dump(resolved), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--base", default="yolo11s.pt")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--project", default="runs/field")
    parser.add_argument("--name", default="numbers")
    args = parser.parse_args()

    from ultralytics import YOLO

    data = resolved_data_yaml(args.data)
    model = YOLO(args.base)
    model.train(
        data=str(data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=0,
        project=args.project,
        name=args.name,
        exist_ok=True,
        # A mirrored frame swaps left/right of midfield: never flip.
        fliplr=0.0,
        patience=15,
        workers=4,
        # Ultralytics' results plot crashes when polars is unavailable (it
        # is on the worker's pinned stack), after the weights are saved.
        plots=False,
    )
    metrics = model.val(data=str(data), split="test", plots=False)
    summary = {"map50": float(metrics.box.map50), "map50_95": float(metrics.box.map)}
    best = Path(args.project) / args.name / "weights" / "best.pt"
    print(json.dumps({"best": str(best), "test": summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
