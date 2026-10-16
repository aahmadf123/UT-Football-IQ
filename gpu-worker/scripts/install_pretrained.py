"""Install the existing YOLOv8n baseline with a pinned source and SHA256.

No model is checked into the repository. This installs generic COCO person
weights; it does not establish football, player-identity or ball accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

SOURCE = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt"
SHA256 = "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36"


def install(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as target:
            temporary = Path(target.name)
            with urllib.request.urlopen(SOURCE, timeout=120) as response:  # noqa: S310
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(block)
                    target.write(block)
        if digest.hexdigest() != SHA256:
            raise ValueError("Pretrained detector checksum mismatch; refusing installation")
        temporary.chmod(0o644)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify(output: Path) -> None:
    """Require real CPU inference before advertising the installed detector."""
    import numpy as np
    import torch
    from ultralytics import YOLO

    torch.set_num_threads(1)
    # This blank image is a runtime smoke check, never a film artifact/label.
    YOLO(str(output)).predict(np.zeros((64, 64, 3), dtype=np.uint8), device="cpu", verbose=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.verify_only:
        verify(args.output)
    else:
        install(args.output)
    print("Pretrained CPU detector verified" if args.verify_only else "Pinned detector installed")


if __name__ == "__main__":
    main()
