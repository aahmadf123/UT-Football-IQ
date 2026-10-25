"""Assemble the metadata-only staff package without copying any film or weights."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


PROPOSAL_ARCHIVE = "runs/practice-detection-review-20261021"
MAX_JSON_BYTES = 32 * 1024 * 1024


def regular_input(path: Path) -> None:
    root = ROOT.resolve()
    if not path.absolute().is_relative_to(root) or not path.resolve().is_relative_to(
        root
    ):
        raise ValueError("Bundle inputs must be regular files inside the workspace")
    if any(part.is_symlink() for part in [path, *path.parents] if part != root):
        raise ValueError("Bundle inputs cannot contain symlinks")
    if not path.is_file():
        raise ValueError(f"Missing regular bundle input: {path.name}")


def json_snapshot(path: Path) -> tuple[bytes, dict]:
    regular_input(path)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("Proposal JSON exceeds the viewer size bound")
    content = path.read_bytes()
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid proposal JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Proposal JSON must contain an object")
    return content, value


def proposal_files(
    directory: Path, catalog: dict, snapshots: dict[Path, bytes] | None = None
) -> list[tuple[Path, str]]:
    """Validate only declared unreviewed metadata using the actual viewer parser."""
    directory = directory.absolute()
    inventory_path = directory / "inventory.json"
    if not inventory_path.is_file():
        raise ValueError("Missing proposal inventory.json")
    captured = {} if snapshots is None else snapshots
    captured[inventory_path], inventory = json_snapshot(inventory_path)
    rows = inventory.get("rows")
    if (
        inventory.get("evidence_kind") != "local_detection_review_inventory"
        or not isinstance(rows, list)
        or not rows
        or len(rows) > 1000
    ):
        raise ValueError("Invalid proposal inventory")
    sources = {source["id"]: source for source in catalog["sources"]}
    entries = [(inventory_path, f"{PROPOSAL_ARCHIVE}/inventory.json")]
    checks = []
    seen = set()
    for row in rows:
        source_id = row.get("source_id") if isinstance(row, dict) else None
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", source_id)
            or source_id not in sources
        ):
            raise ValueError("Inventory source ID must match a safe catalog source")
        if source_id.casefold() in seen:
            raise ValueError("Inventory source IDs must be unique")
        seen.add(source_id.casefold())
        source = sources[source_id]
        if (
            not re.fullmatch(r"[a-f0-9]{64}", str(source.get("media_sha256", "")))
            or row.get("media_sha256") != source["media_sha256"]
        ):
            raise ValueError("Inventory media hash must match the catalog")
        path = directory / f"{source_id}.json"
        captured[path], value = json_snapshot(path)
        if hashlib.sha256(captured[path]).hexdigest() != row.get("proposal_sha256"):
            raise ValueError("Proposal inventory checksum mismatch")
        provenance = value.get("provenance")
        if (
            value.get("reviewed") not in (None, False)
            or not isinstance(provenance, dict)
            or provenance.get("review_status") != "unreviewed"
            or provenance.get("source") != "experimental"
        ):
            raise ValueError(
                "Only explicitly unreviewed experimental proposals may be bundled"
            )
        entries.append((path, f"{PROPOSAL_ARCHIVE}/{path.name}"))
        checks.append({"value": value, "source": source})
    expected = {path.name for path, _ in entries}
    if any(path.name not in expected for path in directory.iterdir()):
        raise ValueError("Unexpected sibling in proposal directory")
    script = """import fs from 'node:fs'; import {pathToFileURL} from 'node:url';
const {detectionProposals}=await import(pathToFileURL(process.argv[1]).href);
for(const item of JSON.parse(fs.readFileSync(0,'utf8'))) detectionProposals(item.value,item.source);
"""
    try:
        subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                script,
                str(ROOT / "tools/football-labeler/connection.mjs"),
            ],
            input=json.dumps(checks),
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(
            "Actual viewer proposal parser rejected metadata or Node.js is unavailable"
        ) from exc
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--proposals",
        type=Path,
        help="Include validated unreviewed detection metadata only",
    )
    args = parser.parse_args()
    paths = []
    for folder, suffixes in [
        ("tools/football-labeler", {".html", ".css", ".mjs", ".py", ".md", ".cmd"}),
        ("docs/labeling", {".md"}),
        ("reports/overhaul/labeling-package", {".json", ".csv"}),
    ]:
        paths.extend(
            path
            for path in (ROOT / folder).rglob("*")
            if path.is_file()
            and path.suffix in suffixes
            and "__pycache__" not in path.parts
        )
    paths.extend(
        ROOT / value
        for value in [
            "scripts/prepare_overhaul_labeling.py",
            "scripts/evaluate_overhaul_release.py",
            "scripts/build_labeling_bundle.py",
            "docs/overhaul-release-evidence.md",
            "reports/overhaul/expected-outcomes-continuation.md",
            "reports/overhaul/expected-outcomes-http-verification.json",
            "reports/overhaul/model-reliability-continuation.md",
            "reports/overhaul/drone-original-inventory.json",
            "reports/overhaul/drone-original-inventory.md",
            "tests_local/test_overhaul_labeling.py",
            "tests_local/test_overhaul_release.py",
            "tests_local/test_labeler_frames.py",
            "tests_local/test_labeler_server.py",
            "tests_local/test_labeling_bundle.py",
            "tests_local/labeler_browser_fixture.py",
            "reports/platform-overhaul-source-inventory-2026-10-21.json",
            "reports/platform-overhaul-release-evidence.json",
        ]
    )
    catalog_filename = (
        "sources-content-verified.json"
        if (
            ROOT / "reports/overhaul/labeling-package/sources-content-verified.json"
        ).is_file()
        else "sources.json"
    )
    entries = [(path, path.relative_to(ROOT).as_posix()) for path in sorted(set(paths))]
    snapshots = {}
    if args.proposals:
        catalog_path = ROOT / "reports/overhaul/labeling-package" / catalog_filename
        snapshots[catalog_path], catalog = json_snapshot(catalog_path)
        entries.extend(proposal_files(args.proposals, catalog, snapshots))
    manifest = {
        "schema_version": 1,
        "status": "unreviewed_staff_labeling_package",
        "media_included": False,
        "model_weights_included": False,
        "proposal_status": "unreviewed" if args.proposals else "not_included",
        "files": [],
    }
    payloads = []
    archive_names = set()
    for path, name in entries:
        if name.casefold() in archive_names:
            raise ValueError(
                "Bundle archive names must be unique on all supported platforms"
            )
        archive_names.add(name.casefold())
        regular_input(path)
        content = snapshots[path] if path in snapshots else path.read_bytes()
        manifest["files"].append(
            {
                "path": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
        payloads.append((name, content))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    proposal_args = (
        f" --proposals {PROPOSAL_ARCHIVE} --library practice" if args.proposals else ""
    )
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in payloads:
            archive.writestr(name, content)
        archive.writestr("bundle-manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.writestr(
            "START-HERE.txt",
            f"Football-IQ staff labeling package\n\nExtract this archive into a new folder. No film or model weights are included.\nRead docs/labeling/README.md and tools/football-labeler/README.md.\n\nFrom the extracted folder run:\n  python tools/football-labeler/server.py --catalog reports/overhaul/labeling-package/{catalog_filename} --drafts runs/staff-review{proposal_args} --port 8042 --open\nOr double-click tools/football-labeler/start-labeler.cmd for the supplied practice workflow.\nThe local server opens catalog originals and saves unreviewed revisioned drafts. Choose the exact original film locally if it moved. If using another computer, update its catalog path; never substitute a different same-name recording.\n\nBundled detection hints are unreviewed experimental person boxes, not football truth or reliable referee, side or identity labels. Accept or edit each box explicitly; hints never complete a review.\n\nThe catalog, drafts and assignments are unreviewed. No accuracy or release gate passes from these templates. Export separate staff passes and use the review/hash/freeze instructions.\n",
        )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "files": len(manifest["files"]) + 2,
                "bytes": args.output.stat().st_size,
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "media_included": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
