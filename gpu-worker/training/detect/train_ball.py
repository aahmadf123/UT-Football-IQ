"""Fine-tune the dedicated single-class ball detector (nano).

The ball pipeline runs a separate nano model over 128 px SAHI tiles
(``pipeline/detection/ball_detector.py``), so this trains a small model on a
ball-only dataset version (generate one in Roboflow filtered to the ``ball``
class).

HONESTY GATE: the ball is non-observable in 720p drone practice footage
(docs/cv/ball-observability.md) — do NOT train or evaluate ball weights on
that corpus and expect signal. Use >=1080p game/sideline footage per
docs/capture-guidance.md.

Usage:
    python -m training.detect.train_ball --data <ball-dataset>/data.yaml
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--base", default="yolov8n.pt")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--name", default="footiq-ball")
    args = parser.parse_args()

    import yaml

    data = yaml.safe_load(args.data.read_text(encoding="utf-8"))
    raw_names = data.get("names", [])
    names = list(raw_names.values()) if isinstance(raw_names, dict) else list(raw_names)
    if names != ["ball"]:
        raise SystemExit(
            f"Ball training expects a single-class dataset ['ball'], got {names}. "
            "Generate a Roboflow version filtered to the ball class."
        )

    from ultralytics import YOLO

    model = YOLO(args.base)
    results = model.train(
        data=str(args.data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        name=args.name,
        # The ball is tiny; keep aggressive mosaic + copy-paste off so real
        # scale statistics survive.
        mosaic=0.5,
    )
    best = Path(results.save_dir) / "weights" / "best.pt"
    print(f"\nBest weights: {best}")
    print("Gate promotion on eval/ball_benchmark.py before registering.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
