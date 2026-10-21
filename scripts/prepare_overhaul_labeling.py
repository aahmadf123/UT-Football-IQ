"""Prepare unreviewed staff labeling drafts; freeze only explicit human review.

All media reads use bounded streaming. This never copies footage, invents play
boundaries, confirms independent groups, or writes over an existing artifact.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "reports/platform-overhaul-source-inventory-2026-10-21.json"
DEFAULT_OUTPUT = ROOT / "reports/overhaul/labeling-package"
CATEGORIES = ("practice", "coach_game", "television")
PASSES = ("primary", "secondary", "adjudication")
TARGETS = {"canonical_plays": 240, "independent_groups": 12,
           "plays_per_category": 80, "independent_groups_per_category": 4,
           "dense_track_plays": 48, "dense_track_plays_per_category": 16,
           "double_labeled_adjudicated_fraction": 0.2,
           "accepted_identity_cases_target": 2000,
           "identity_precision_minimum": 0.995, "identity_wilson_95_lower_minimum": 0.99,
           "readable_team_resolved_identity_coverage_minimum": 0.9}
DENSE_COVERAGE_DRAFT = {"schema_version": 1, "evidence_kind": "dense_coverage_draft",
                        "source_id": None, "media_sha256": None, "source_play_id": None,
                        "canonical_play_id": None, "completed": False,
                        "all_visible_players_annotated": False, "occlusion_offscreen_records_reviewed": False,
                        "reviewer_ids": [], "native_frame_index": {"path": None, "sha256": None}, "frames": [],
                        "notes": "Unreviewed scaffold. Populate actual assigned native-frame coverage; human review is never inferred from this template."}


def read_json(path: Path) -> dict[str, Any]:
    def reject(value: str) -> None:
        raise ValueError(f"Non-finite JSON number: {value}")

    packet = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject)
    if not isinstance(packet, dict):
        raise TypeError("Expected a JSON object")
    return packet


def write_new(path: Path, packet: Any) -> None:
    encoded = json.dumps(packet, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also refuses symlink/hardlink aliases of real evidence.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def stream_hash(path: Path, expected_bytes: int | None = None) -> str:
    before = path.stat()
    if expected_bytes is not None and before.st_size != expected_bytes:
        raise ValueError(f"Source size changed since inventory: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f"Source changed while hashing: {path.name}")
    return digest.hexdigest()


def finite(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def named(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def make_catalog(inventory: dict[str, Any]) -> dict[str, Any]:
    sources = []
    seen: set[str] = set()
    for item in inventory.get("items", []):
        filename = item.get("name") or PureWindowsPath(item["path"]).name
        library = item.get("library", "unknown")
        # Moving a library to a different staff computer does not change IDs.
        key = f"{library}/{filename}".casefold()
        sid = "source-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        if sid in seen:
            raise ValueError(f"Duplicate source filename within library: {filename}")
        seen.add(sid)
        probe = item.get("probe") or {}
        duration = float((probe.get("format") or {}).get("duration", 0))
        video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        width, height = video.get("width"), video.get("height")
        if (not finite(duration) or duration <= 0 or not isinstance(item.get("bytes"), int)
                or isinstance(item["bytes"], bool) or item["bytes"] <= 0):
            raise ValueError(f"Missing positive probed duration/byte size: {filename}")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (width, height)):
            raise ValueError(f"Missing positive probed source video dimensions: {filename}")
        date = re.search(r"Dji (20\d{6})", filename, re.IGNORECASE)
        if library == "practice" and date:
            digits = date.group(1)
            group = f"practice-{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
            basis = "Filename date; all extracts from this proposed session stay together."
        else:
            group = f"proposed-{library}-{sid.removeprefix('source-')}"
            basis = "One file proposal; staff must identify the actual game/session and all related views."
        sources.append({"id": sid, "filename": filename, "path": item["path"],
                        "bytes": item["bytes"], "duration_seconds": duration,
                        "width": width, "height": height,
                        "r_frame_rate": video.get("r_frame_rate"), "avg_frame_rate": video.get("avg_frame_rate"),
                        "category": "practice" if library == "practice" else None,
                        "category_status": "proposed", "group_id": group,
                        "group_status": "proposed", "source_context_confirmed": False,
                        "group_basis": basis, "media_sha256": None, "hash_status": "hash_pending",
                        "library": library, "inventory_observation_date": inventory.get("observed_date")})
    if not sources:
        raise ValueError("Inventory has no sources")
    return {"schema_version": 1, "evidence_kind": "source_catalog", "sources": sorted(sources, key=lambda s: (s["library"], s["filename"])),
            "notes": ["Groups and film categories require staff confirmation; filenames do not prove independence.",
                      "No source hashes or human labels are claimed by this preparation step."]}


def draft_for(source: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "evidence_kind": "annotation_draft", "reviewed": False,
            "source_id": source["id"], "media_sha256": source["media_sha256"],
            "source_category": source["category"], "source_group_id": source["group_id"],
            "source_context_confirmed": False, "reviewer_id": "", "review_pass": "primary", "plays": [],
            "notes": "Unreviewed empty template. Add only observations verified against original source time."}


def prepare(inventory_path: Path, output: Path) -> dict[str, Any]:
    catalog = make_catalog(read_json(inventory_path))
    if output.exists():
        raise ValueError("Output already exists; use a new directory to preserve staff edits")
    output.mkdir(parents=True)
    write_new(output / "sources.json", catalog)
    write_new(output / "dense-coverage-draft.json", DENSE_COVERAGE_DRAFT)
    fields = ["id", "filename", "path", "bytes", "duration_seconds", "width", "height", "r_frame_rate", "avg_frame_rate", "category", "category_status",
              "group_id", "group_status", "media_sha256", "hash_status"]
    with (output / "source-register.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(catalog["sources"])
    with (output / "review-assignments.csv").open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["source_id", "filename", "group_id", "review_pass",
                                                   "reviewer_id", "status", "template", "notes"])
        writer.writeheader()
        for source in catalog["sources"]:
            template = f"drafts/{source['id']}.json"
            write_new(output / template, draft_for(source))
            for review_pass in PASSES:
                writer.writerow({"source_id": source["id"], "filename": source["filename"], "group_id": source["group_id"],
                                 "review_pass": review_pass, "reviewer_id": "", "status": "unassigned",
                                 "template": template, "notes": "Save separate independent pass files; never overwrite the primary draft."})
    proposed = len({s["group_id"] for s in catalog["sources"]})
    readiness = {"schema_version": 1, "status": "unreviewed_not_release_evidence", "targets": TARGETS,
                 "source_count": len(catalog["sources"]), "proposed_groups": proposed, "confirmed_groups": 0,
                 "proposed_categories": dict(Counter(s["category"] or "unconfirmed_game_format" for s in catalog["sources"])),
                 "minimum_additional_groups_if_all_proposals_confirmed": max(0, 12 - proposed),
                 "confirmed_groups_by_category": dict.fromkeys(CATEGORIES, 0),
                 "missing_confirmed_groups_by_category": dict.fromkeys(CATEGORIES, 4),
                 "limitations": ["Thirty same-date practice extracts propose one session, not thirty independent groups.",
                                 "Six game files do not establish six independent games or either game-film category until staff review.",
                                 "No television category has been identified. Obtain independent television sources if these files do not supply them.",
                                 "Targets refer to distinct canonical plays, source groups and athlete cases, not clip or alternate-view counts.",
                                 "No annotation, model evaluation, split assignment or acceptance gate passes from this package."]}
    write_new(output / "readiness.json", readiness)
    write_new(output / "split-plan.json", {"schema_version": 1, "state": "staff_group_confirmation_required",
                                           "splits": {"train": [], "calibration": [], "holdout": []},
                                           "proposed_group_ids": sorted({s["group_id"] for s in catalog["sources"]}),
                                           "notes": ["Confirm independent session/game groups before choosing splits.",
                                                     "Keep all extracts, replays and alternate views from each group in one split.",
                                                     "Do not tune on the final held-out benchmark; reserve separate training/calibration groups."]})
    return readiness


def source_for(catalog: dict[str, Any], source_id: str) -> dict[str, Any]:
    matches = [s for s in catalog.get("sources", []) if s.get("id") == source_id]
    if len(matches) != 1:
        raise ValueError("Draft must identify one unique catalog source")
    return matches[0]


def hash_catalog(catalog: dict[str, Any], selected: list[str]) -> dict[str, Any]:
    result = copy.deepcopy(catalog)
    ids = selected or [s["id"] for s in result["sources"]]
    for source_id in ids:
        source = source_for(result, source_id)
        source["media_sha256"] = stream_hash(Path(source["path"]), source.get("bytes"))
        source["hash_status"] = "verified_local_bytes"
    return result


def validate_draft(draft: dict[str, Any], source: dict[str, Any], review_pass: str, digest: str) -> None:
    if (draft.get("evidence_kind") != "annotation_draft" or draft.get("reviewed") is not False
            or draft.get("review_pass") != review_pass or draft.get("source_id") != source["id"]
            or not isinstance(draft.get("reviewer_id"), str) or not draft["reviewer_id"].strip()):
        raise ValueError(f"A named {review_pass} reviewer and matching unreviewed source draft are required")
    if (draft.get("source_context_confirmed") is not True or draft.get("source_category") != source["category"]
            or draft.get("source_group_id") != source["group_id"]):
        raise ValueError("Draft source category/group must match the confirmed catalog")
    if draft.get("media_sha256") not in (None, digest):
        raise ValueError("Draft hash does not match actual source bytes")
    plays = draft.get("plays")
    if not isinstance(plays, list) or not plays:
        raise ValueError("Draft must contain actual reviewed plays")
    ids = set()
    for play in plays:
        if not isinstance(play, dict) or not named(play.get("id")) or not named(play.get("canonical_play_id")):
            raise ValueError("Every play needs source-local and canonical IDs")
        if play["id"] in ids:
            raise ValueError("Duplicate source-local play IDs within source draft")
        ids.add(play["id"])
        bounds = [play.get(key) for key in ("start_seconds", "snap_seconds", "end_seconds")]
        if (not finite(bounds[0]) or not finite(bounds[2]) or not 0 <= bounds[0] < bounds[2] <= source["duration_seconds"]
                or (bounds[1] is not None and (not finite(bounds[1]) or not bounds[0] <= bounds[1] < bounds[2]))):
            raise ValueError("Play boundaries/snap must be finite original-source seconds within the recording")
        target_ids = set()
        for track in play.get("dense_tracks", []):
            points = track.get("points", [])
            if not named(track.get("target_id")) or not points:
                raise ValueError("Tracks need an actual target and at least one visible observation")
            if track["target_id"] in target_ids:
                raise ValueError("Duplicate target IDs within play window")
            target_ids.add(track["target_id"])
            previous = -1.0
            for point in points:
                stamp, box = point.get("timestamp_seconds"), point.get("bbox")
                if (not finite(stamp) or not bounds[0] <= stamp <= bounds[2] or stamp <= previous
                        or point.get("visibility_state", "visible") != "visible"
                        or not isinstance(box, list) or len(box) != 4 or not all(finite(v) and v >= 0 for v in box)
                        or box[2] <= box[0] or box[3] <= box[1]
                        or box[2] > source["width"] or box[3] > source["height"]):
                    raise ValueError("Dense points require increasing source time and a real visible pixel box; put losses in visibility_events")
                previous = stamp
            for marker in track.get("visibility_events", []):
                stamp = marker.get("timestamp_seconds")
                if (not finite(stamp) or not bounds[0] <= stamp <= bounds[2]
                        or marker.get("visibility_state") not in {"occluded", "offscreen"}):
                    raise ValueError("Visibility events need an observed loss state and source time")
        identity_targets = set()
        for identity in play.get("identities", []):
            target = identity.get("target_id")
            if target not in target_ids or target in identity_targets:
                raise ValueError("Identity must refer to one unique actual target in this play window")
            identity_targets.add(target)
            entity = identity.get("canonical_entity_id")
            if (not isinstance(identity.get("readable"), bool) or not isinstance(identity.get("team_resolved"), bool)
                    or (entity is not None and not named(entity))):
                raise ValueError("Identity requires explicit readability/team flags and a named or unknown canonical athlete case")
            player = identity.get("player_id")
            if player is not None and (not named(player) or not named(entity) or not identity["team_resolved"]):
                raise ValueError("Known roster identity requires a player ID, canonical athlete case and resolved team; preserve unknowns as null")
        for event in play.get("events", []):
            if (not isinstance(event.get("event_type"), str) or not event["event_type"].strip()
                    or not finite(event.get("timestamp_seconds"))
                    or not bounds[0] <= event["timestamp_seconds"] <= bounds[2]):
                raise ValueError("Events require a named observed event and source timestamp within the play")


def freeze(catalog: dict[str, Any], primary: dict[str, Any], *, attest_reviewed: bool,
           secondary: dict[str, Any] | None = None, adjudication: dict[str, Any] | None = None,
           double_plays: list[str] | None = None, attest_adjudicated: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    if not attest_reviewed:
        raise ValueError("Explicit --attest-reviewed is required after actual human review")
    source = source_for(catalog, primary.get("source_id", ""))
    confirmed = source.get("source_context_confirmed") is True or (
        source.get("group_status") == "confirmed" and source.get("category_status") == "confirmed"
    )
    if not confirmed or source.get("category") not in CATEGORIES or not source.get("group_id"):
        raise ValueError("Staff must confirm source category and independent session/game group first")
    digest = stream_hash(Path(source["path"]), source.get("bytes"))
    if source.get("media_sha256") not in (None, digest):
        raise ValueError("Catalog hash does not match actual source bytes")
    validate_draft(primary, source, "primary", digest)
    selected = set(double_plays or [])
    plays = copy.deepcopy(primary["plays"])
    reviewer_ids = [primary["reviewer_id"].strip()]
    if selected:
        if secondary is None or adjudication is None or not attest_adjudicated:
            raise ValueError("Double-label claims require independent secondary/adjudication drafts and --attest-adjudicated")
        validate_draft(secondary, source, "secondary", digest)
        validate_draft(adjudication, source, "adjudication", digest)
        if primary["reviewer_id"].strip() == secondary["reviewer_id"].strip():
            raise ValueError("Primary and secondary reviewers must be distinct people")
        by_pass = [{p["id"]: p for p in draft["plays"]} for draft in (primary, secondary, adjudication)]
        for canonical in selected:
            occurrences = [{p["id"] for p in mapping.values() if p["canonical_play_id"] == canonical} for mapping in by_pass]
            if not occurrences[0] or not occurrences[0] == occurrences[1] == occurrences[2]:
                raise ValueError("Every double-labeled canonical play needs all source-local occurrences in both independent passes and adjudication")
        plays = [copy.deepcopy(by_pass[2][p["id"]]) if p["canonical_play_id"] in selected else p for p in plays]
        reviewer_ids = list(dict.fromkeys([*reviewer_ids, secondary["reviewer_id"].strip(), adjudication["reviewer_id"].strip()]))
    elif secondary is not None or adjudication is not None or attest_adjudicated:
        raise ValueError("Name canonical --double-play IDs to make explicit double-label claims")
    for play in plays:
        claimed = play.get("double_labeled") is True or play.get("adjudicated") is True
        double = play["canonical_play_id"] in selected
        if claimed and not double:
            raise ValueError("A draft checkbox alone cannot establish double review/adjudication")
        play["double_labeled"] = double
        play["adjudicated"] = double
        play["reviewer_ids"] = reviewer_ids if double else [primary["reviewer_id"].strip()]
    validate_draft({**primary, "plays": plays}, source, "primary", digest)
    eligible, sequences = [], []
    for play in plays:
        if play.get("snap_seconds") is not None and play.get("canonical_count_eligible") is not False:
            eligible.append(play)
        else:
            play["canonical_count_eligible"] = False
            play["sequence_reason"] = play.get("sequence_reason") or (
                "no_observed_snap" if play.get("snap_seconds") is None else "staff_non_countable_sequence"
            )
            sequences.append(play)
    packet = {"schema_version": 1, "evidence_kind": "human_reviewed", "reviewed": True,
              "reviewer_ids": reviewer_ids, "source_id": source["id"], "media_sha256": digest,
              "category": source["category"], "group_id": source["group_id"], "plays": eligible, "sequences": sequences,
              "frozen_at": datetime.now(UTC).isoformat(),
              "review_attestation": {"human_review_explicit": True, "adjudication_explicit": bool(selected)},
              "notes": "Human-reviewed annotation snapshot; accuracy evaluation and release gates remain separate."}
    entry = {"id": source["id"], "group_id": source["group_id"], "category": source["category"],
             "media": {"path": str(Path(source["path"]).resolve()), "sha256": digest}}
    return packet, entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Create a new unreviewed package from the probed inventory")
    prep.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    prep.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    hashes = commands.add_parser("hash", help="Stream actual source SHA-256 into a separate catalog")
    hashes.add_argument("--catalog", type=Path, required=True)
    hashes.add_argument("--source-id", action="append", default=[])
    hashes.add_argument("--output", type=Path, required=True)
    frozen = commands.add_parser("freeze", help="Freeze actual reviewed labels; explicit review attestation required")
    frozen.add_argument("--catalog", type=Path, required=True)
    frozen.add_argument("--draft", type=Path, required=True)
    frozen.add_argument("--secondary", type=Path)
    frozen.add_argument("--adjudication", type=Path)
    frozen.add_argument("--double-play", action="append", default=[])
    frozen.add_argument("--attest-reviewed", action="store_true")
    frozen.add_argument("--attest-adjudicated", action="store_true")
    frozen.add_argument("--output", type=Path, required=True)
    frozen.add_argument("--entry-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.inventory, args.output)
        else:
            catalog = read_json(args.catalog)
            outputs = [args.output] + ([args.entry_output] if args.command == "freeze" else [])
            if len({p.resolve() for p in outputs}) != len(outputs) or any(p.exists() for p in outputs):
                raise ValueError("Outputs must be distinct new paths; existing staff artifacts are never overwritten")
            if args.command == "hash":
                write_new(args.output, hash_catalog(catalog, args.source_id))
                result = {"status": "hashed_catalog_written", "output": str(args.output)}
            else:
                packet, entry = freeze(catalog, read_json(args.draft), attest_reviewed=args.attest_reviewed,
                                       secondary=read_json(args.secondary) if args.secondary else None,
                                       adjudication=read_json(args.adjudication) if args.adjudication else None,
                                       double_plays=args.double_play, attest_adjudicated=args.attest_adjudicated)
                write_new(args.output, packet)
                entry["labels"] = {"path": str(args.output.resolve()), "sha256": stream_hash(args.output)}
                write_new(args.entry_output, entry)
                result = {"status": "human_reviewed_labels_frozen", "output": str(args.output), "source_entry": str(args.entry_output)}
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
