"""Additive worker dispatch compatibility and startup checkout inventory."""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

PRODUCER_PROTOCOL = 1
TRAINING_VARIANTS = ("local_coverage", "expected_outcomes", "verify_expected_outcomes")


@lru_cache(maxsize=1)
def source_revision() -> dict[str, Any]:
    """Cache the checkout seen at startup, not a proof of later imported bytes."""
    root = Path(__file__).resolve().parents[1]
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            return {"git_sha": "unknown", "git_dirty": None}
        dirty = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        return {"git_sha": sha, "git_dirty": bool(dirty.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"git_sha": "unknown", "git_dirty": None}
