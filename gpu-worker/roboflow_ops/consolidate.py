"""Consolidate the legacy Roboflow projects into the canonical one.

For each legacy project: download its latest dataset version (COCO), remap
every annotation onto the locked taxonomy (``roboflow_ops.taxonomy``), then
upload image + per-image COCO annotation into the consolidated project
(``ROBOFLOW_PROJECT``) with ``src:<project>`` provenance tags.

Idempotent: uploaded image content hashes are recorded in a manifest under
``ROBOFLOW_DATA_DIR`` and skipped on re-runs (Roboflow also dedupes
identical images server-side — the manifest just saves the bandwidth).
Legacy projects are never modified or deleted; consolidation is auditable
and reversible.

The consolidated project's OWN legacy images (american-football-analyst) are
not re-uploaded — its native annotations are remapped at version-generation
time with Roboflow's Modify Classes step instead (see
docs/cv/roboflow-workflow.md).

Usage:
    python -m roboflow_ops.consolidate [--projects slug1,slug2] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import structlog

from roboflow_ops.client import (
    LEGACY_PROJECTS,
    RoboflowConfig,
    get_project,
    load_config,
    upload_image,
)
from roboflow_ops.taxonomy import RemapStats, remap_coco

log = structlog.get_logger(__name__)

_MANIFEST_NAME = "consolidate-manifest.json"


def _manifest_path(cfg: RoboflowConfig) -> Path:
    return cfg.data_dir / _MANIFEST_NAME


def _load_manifest(cfg: RoboflowConfig) -> set[str]:
    path = _manifest_path(cfg)
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get("uploaded_hashes", []))


def _save_manifest(cfg: RoboflowConfig, hashes: set[str]) -> None:
    _manifest_path(cfg).write_text(
        json.dumps({"uploaded_hashes": sorted(hashes)}, indent=2), encoding="utf-8"
    )


def _download_latest_version(cfg: RoboflowConfig, slug: str) -> Path | None:
    """Download the newest generated version of a project as COCO; None if none."""
    project = get_project(cfg, slug)
    try:
        versions = project.versions()
    except Exception as exc:
        log.warning("roboflow_versions_list_failed", project=slug, error=str(exc)[:200])
        versions = []
    if not versions:
        log.warning(
            "roboflow_no_versions",
            project=slug,
            hint="Generate a dataset version in the Roboflow UI first.",
        )
        return None
    newest = max(versions, key=lambda v: int(str(v.version).rsplit("/", 1)[-1]))
    target = cfg.data_dir / "exports" / slug
    target.mkdir(parents=True, exist_ok=True)
    dataset = newest.download(model_format="coco", location=str(target), overwrite=True)
    return Path(dataset.location)


def _iter_coco_splits(dataset_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Yield (split_dir, coco_dict) for every _annotations.coco.json found."""
    out = []
    for ann_file in sorted(dataset_dir.rglob("_annotations.coco.json")):
        out.append((ann_file.parent, json.loads(ann_file.read_text(encoding="utf-8"))))
    return out


def _single_image_coco(coco: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
    anns = [a for a in coco["annotations"] if a["image_id"] == image["id"]]
    return {
        "info": coco.get("info", {}),
        "images": [image],
        "annotations": anns,
        "categories": coco["categories"],
    }


def consolidate_project(
    cfg: RoboflowConfig,
    slug: str,
    uploaded_hashes: set[str],
    *,
    dry_run: bool,
) -> tuple[RemapStats, int, int]:
    """Export, remap, and upload one legacy project. Returns (stats, uploaded, skipped)."""
    dataset_dir = _download_latest_version(cfg, slug)
    if dataset_dir is None:
        return RemapStats(), 0, 0

    target_project = None if dry_run else get_project(cfg)
    total_stats = RemapStats()
    uploaded = 0
    skipped = 0

    for split_dir, coco in _iter_coco_splits(dataset_dir):
        remapped, stats = remap_coco(coco)
        # Accumulate in place — a replacement comprehension would drop counts
        # for classes absent from the current split.
        for name, count in stats.kept.items():
            total_stats.kept[name] = total_stats.kept.get(name, 0) + count
        for name, count in stats.dropped.items():
            total_stats.dropped[name] = total_stats.dropped.get(name, 0) + count

        for image in remapped["images"]:
            image_path = split_dir / image["file_name"]
            if not image_path.exists():
                continue
            digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if digest in uploaded_hashes:
                skipped += 1
                continue
            if dry_run:
                uploaded += 1
                uploaded_hashes.add(digest)
                continue
            ann_path = split_dir / f"{image_path.stem}.remapped.coco.json"
            ann_path.write_text(json.dumps(_single_image_coco(remapped, image)), encoding="utf-8")
            ok = upload_image(
                target_project,
                image_path,
                batch_name=f"consolidated-{slug}",
                tags=[f"src:{slug}"],
                annotation_path=ann_path,
            )
            if ok:
                uploaded += 1
                uploaded_hashes.add(digest)

    return total_stats, uploaded, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--projects",
        default=",".join(p for p in LEGACY_PROJECTS),
        help="Comma-separated legacy project slugs to consolidate",
    )
    parser.add_argument("--dry-run", action="store_true", help="Remap + count, no uploads")
    args = parser.parse_args()

    cfg = load_config()
    slugs = [s.strip() for s in args.projects.split(",") if s.strip()]
    # Never re-import the consolidated project into itself.
    slugs = [s for s in slugs if s != cfg.project]

    uploaded_hashes = _load_manifest(cfg)
    grand_uploaded = 0
    grand_skipped = 0
    for slug in slugs:
        print(f"\n=== {slug} ===")
        stats, uploaded, skipped = consolidate_project(
            cfg, slug, uploaded_hashes, dry_run=args.dry_run
        )
        print(stats.table())
        print(f"images uploaded: {uploaded} · skipped (already uploaded): {skipped}")
        grand_uploaded += uploaded
        grand_skipped += skipped
        if not args.dry_run:
            _save_manifest(cfg, uploaded_hashes)

    print(f"\nTotal uploaded: {grand_uploaded} · total skipped: {grand_skipped}")
    if args.dry_run:
        print("(dry run — nothing was uploaded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
