"""Auto-label helper: the primary labeling flow is grounded auto-label + review.

Nobody hand-draws boxes from scratch. Frames uploaded by
``roboflow_ops.frames`` get labeled by Roboflow's hosted auto-label
(SAM-family / grounded prompts) and the result lands in an annotation job for
a quick human review pass — approve/fix beats draw-from-zero by an order of
magnitude.

The auto-label REST surface is not exposed by the ``roboflow`` pip SDK, so
this CLI drives the documented labeling endpoint directly; if the endpoint
shape changes, it prints the exact app URL to run the same job from the UI
(Annotate → select batch → Auto Label) with the canonical prompts below.

Prompts are pinned to the locked taxonomy — bare nouns work best:
    player   -> "football player"
    official -> "referee"
    ball     -> "football"

Usage:
    python -m roboflow_ops.autolabel --batch <batch-name-or-id>
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import structlog

from roboflow_ops.client import RoboflowConfig, load_config

log = structlog.get_logger(__name__)

# Class → grounding prompt (bare nouns; SAM-family grounding likes these).
PROMPTS: dict[str, str] = {
    "player": "football player",
    "official": "referee",
    "ball": "football",
}

_API_BASE = "https://api.roboflow.com"


def start_autolabel(cfg: RoboflowConfig, batch_id: str, model: str = "sam3") -> dict[str, Any]:
    """Kick off a hosted auto-label job over a batch. Returns the API response."""
    import httpx

    resp = httpx.post(
        f"{_API_BASE}/{cfg.workspace}/{cfg.project}/jobs",
        params={"api_key": cfg.api_key},
        json={
            "batch": batch_id,
            "type": "autolabel",
            "model": model,
            "ontology": PROMPTS,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


def ui_instructions(cfg: RoboflowConfig, batch: str) -> str:
    return (
        "Run auto-label from the Roboflow UI instead:\n"
        f"  1. Open https://app.roboflow.com/{cfg.workspace}/{cfg.project}/annotate\n"
        f"  2. Select the batch '{batch}' → Auto Label\n"
        "  3. Use these prompts (one class per line):\n"
        + "".join(f"       {cls}: {prompt}\n" for cls, prompt in PROMPTS.items())
        + "  4. Review the proposals, fix what's wrong, approve into the dataset."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, help="Batch name/id to auto-label")
    parser.add_argument("--model", default="sam3", help="Foundational model (default sam3)")
    args = parser.parse_args()

    cfg = load_config()
    try:
        result = start_autolabel(cfg, args.batch, args.model)
        print(json.dumps(result, indent=2))
        print("\nAuto-label started — review the proposals in the Roboflow UI when it finishes.")
    except Exception as exc:
        log.warning("autolabel_api_failed", error=str(exc)[:300])
        print(f"Could not start auto-label via the API ({exc}).\n")
        print(ui_instructions(cfg, args.batch))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
