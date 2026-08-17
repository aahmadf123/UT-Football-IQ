"""Kick off Roboflow-hosted training on a dataset version.

Hosted training is the fast-iteration path: it answers "is the dataset good
enough yet?" without tying up the GPU box. Deployable weights for the
pipeline come from the local path (``training/detect/train_yolo.py``) because
hosted weights are not generally downloadable on every plan — check your
workspace; if your plan allows weight download, prefer whichever run
evaluates better.

Usage:
    python -m roboflow_ops.hosted_train --version 3 [--speed fast]
"""

from __future__ import annotations

import argparse

import structlog

from roboflow_ops.client import get_project, load_config

log = structlog.get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument(
        "--speed",
        default="fast",
        choices=["fast", "accurate"],
        help="Roboflow training recipe speed",
    )
    args = parser.parse_args()

    cfg = load_config()
    project = get_project(cfg)
    version = project.version(args.version)
    try:
        model = version.train(speed=args.speed)
    except Exception as exc:
        log.warning("hosted_train_failed", error=str(exc)[:300])
        print(
            f"Hosted training could not be started ({exc}).\n"
            f"Start it from the UI: https://app.roboflow.com/{cfg.workspace}/{cfg.project}"
            f"/{args.version}"
        )
        return 1
    print(f"Hosted training started for version {args.version}: {model}")
    print("Track progress in the Roboflow UI; evaluate with model_evals when done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
