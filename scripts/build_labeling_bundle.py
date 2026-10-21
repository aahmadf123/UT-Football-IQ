"""Assemble the metadata-only staff package without copying any film or weights."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = []
    for folder, suffixes in [
        ("tools/football-labeler", {".html", ".css", ".mjs", ".py", ".md"}),
        ("docs/labeling", {".md"}),
        ("reports/overhaul/labeling-package", {".json", ".csv"}),
    ]:
        paths.extend(path for path in (ROOT / folder).rglob("*") if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts)
    paths.extend(ROOT / value for value in [
        "scripts/prepare_overhaul_labeling.py", "scripts/evaluate_overhaul_release.py",
        "scripts/build_labeling_bundle.py", "docs/overhaul-release-evidence.md",
        "tests_local/test_overhaul_labeling.py", "tests_local/test_overhaul_release.py",
        "tests_local/test_labeler_frames.py", "reports/platform-overhaul-source-inventory-2026-10-21.json",
        "reports/platform-overhaul-release-evidence.json",
    ])
    manifest = {"schema_version": 1, "status": "unreviewed_staff_labeling_package", "media_included": False, "model_weights_included": False, "files": []}
    for path in sorted(set(paths)):
        if not path.resolve().is_relative_to(ROOT) or path.is_symlink():
            raise ValueError("Bundle inputs must be regular files inside the workspace")
        manifest["files"].append({"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(paths)):
            archive.write(path, path.relative_to(ROOT).as_posix())
        archive.writestr("bundle-manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr("START-HERE.txt", "Football-IQ staff labeling package\n\nExtract this archive into a new folder. No film or model weights are included.\nRead docs/labeling/README.md and tools/football-labeler/README.md.\n\nFrom the extracted folder run:\n  python -m http.server 8030 --bind 127.0.0.1 --directory tools/football-labeler\nOpen http://127.0.0.1:8030 and load reports/overhaul/labeling-package/sources.json.\nChoose the exact original film locally. If using another computer, update its catalog path; never substitute a different same-name recording.\n\nThe catalog, drafts and assignments are unreviewed. No accuracy or release gate passes from these templates. Export separate staff passes and use the review/hash/freeze instructions.\n")
    print(json.dumps({"output": str(args.output), "files": len(manifest["files"]) + 2, "bytes": args.output.stat().st_size, "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(), "media_included": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
