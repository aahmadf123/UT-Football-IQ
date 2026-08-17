"""Roboflow SDK/config wrapper (lazy imports, offline-safe errors).

Environment (gpu-worker only; never read by the backend):
    ROBOFLOW_API_KEY    private API key — never logged, never committed
    ROBOFLOW_WORKSPACE  workspace slug (e.g. ahmads-workspace-zlz9h)
    ROBOFLOW_PROJECT    consolidated project slug
                        (american-football-analyst-dzig0 — repurposed as the
                        single clean-taxonomy project; the workspace's plan
                        caps project count, so a fresh project was not an
                        option)
    ROBOFLOW_DATA_DIR   scratch dir for exports/frames (gitignored)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

LEGACY_PROJECTS: tuple[str, ...] = (
    "ballgame3-v2t4p",
    "football-players-zm06l-kkwtl",
    "american-football-analyst-dzig0",
    "find-american-football-qvszp",
)


class RoboflowConfigError(RuntimeError):
    """Raised when required Roboflow environment configuration is missing."""


@dataclass(frozen=True)
class RoboflowConfig:
    api_key: str
    workspace: str
    project: str
    data_dir: Path


def load_config() -> RoboflowConfig:
    api_key = os.environ.get("ROBOFLOW_API_KEY", "")
    if not api_key:
        raise RoboflowConfigError(
            "ROBOFLOW_API_KEY is not set. Add it to the gpu-worker .env "
            "(see .env.example — the key is private; never commit it)."
        )
    workspace = os.environ.get("ROBOFLOW_WORKSPACE", "")
    if not workspace:
        raise RoboflowConfigError("ROBOFLOW_WORKSPACE is not set (workspace slug).")
    project = os.environ.get("ROBOFLOW_PROJECT", "")
    if not project:
        raise RoboflowConfigError("ROBOFLOW_PROJECT is not set (the consolidated project slug).")
    data_dir = Path(os.environ.get("ROBOFLOW_DATA_DIR", "./roboflow-data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return RoboflowConfig(api_key=api_key, workspace=workspace, project=project, data_dir=data_dir)


def _sdk() -> Any:
    """Import the roboflow SDK lazily with an actionable failure."""
    try:
        import roboflow
    except ImportError as exc:  # pragma: no cover - depends on install
        raise RoboflowConfigError(
            "The 'roboflow' package is not installed. Run: pip install -r requirements-roboflow.txt"
        ) from exc
    return roboflow


def get_workspace(cfg: RoboflowConfig) -> Any:
    rf = _sdk().Roboflow(api_key=cfg.api_key)
    return rf.workspace(cfg.workspace)


def get_project(cfg: RoboflowConfig, slug: str | None = None) -> Any:
    return get_workspace(cfg).project(slug or cfg.project)


def upload_image(
    project: Any,
    image_path: Path,
    *,
    batch_name: str,
    tags: list[str] | None = None,
    annotation_path: Path | None = None,
    retries: int = 3,
) -> bool:
    """Upload one image (optionally with its annotation file) with backoff.

    Returns True on success; failures are logged (without the key) and
    reported as False so callers can tally rather than abort a long run.
    """
    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            kwargs: dict[str, Any] = {
                "image_path": str(image_path),
                "batch_name": batch_name,
                "tag_names": tags or [],
            }
            if annotation_path is not None:
                kwargs["annotation_path"] = str(annotation_path)
            project.upload(**kwargs)
            return True
        except Exception as exc:
            log.warning(
                "roboflow_upload_failed",
                image=image_path.name,
                attempt=attempt,
                error=str(exc)[:300],
            )
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
    return False
