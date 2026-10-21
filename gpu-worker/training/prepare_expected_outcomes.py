"""Freeze actual stage feature exports and staff-entered CSV targets, without inventing labels."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from pipeline.expected_outcomes.data import (
    DataError,
    annotation_artifact,
    validate_dataset,
    validate_snapshot,
)
from training.expected_outcomes import _read_verified, _write_json, file_sha256

TARGET_COLUMNS = (
    "task",
    "canonical_play_id",
    "feature_evidence_sha256",
    "prediction_seconds",
    "value",
    "source",
    "reviewed",
    "reviewer_id",
    "reviewed_at",
    "split",
    "reviewer_ids",
    "double_labeled",
    "adjudicated",
)


def read_snapshots(exports: list[tuple[Path, str]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    found = {}

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            if "payload" in value:
                collect(value["payload"])
            elif "feature_snapshots" in value:
                collect(list(value["feature_snapshots"].values()))
            elif "task" in value and "features" in value:
                snapshot = validate_snapshot(value)
                artifact = value.get("feature_evidence")
                encoded = json.dumps(
                    artifact, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
                digest = hashlib.sha256(encoded).hexdigest()
                if artifact is None or set(snapshot.evidence_hashes) != {digest}:
                    raise DataError("Stage export requires its unchanged typed feature evidence")
                key = snapshot.task, dict(snapshot.lineage)["canonical_play_id"], digest
                if key in found and found[key] != value:
                    raise DataError("Conflicting immutable stage exports")
                found[key] = value
            else:
                raise DataError("Choose an actual expected_outcomes stage export")
        else:
            raise DataError("Malformed stage export")

    for path, digest in exports:
        content = _read_verified(path, digest)
        try:
            document = json.loads(content)
        except json.JSONDecodeError:
            document = [json.loads(line) for line in content.splitlines() if line.strip()]
        collect(document)
    if not found:
        raise DataError("No qualified feature snapshots were exported")
    return found


def targets_template(exports: list[tuple[Path, str]], output: Path) -> dict[str, Any]:
    snapshots = read_snapshots(exports)
    # Exclusive-create also rejects any existing evidence file or alias.
    with output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=TARGET_COLUMNS)
        writer.writeheader()
        for (task, play, digest), row in sorted(snapshots.items()):
            writer.writerow(
                {
                    "task": task,
                    "canonical_play_id": play,
                    "feature_evidence_sha256": digest,
                    "prediction_seconds": row["observation_clock"]["timestamp_seconds"],
                }
            )
    return {"template_path": str(output), "unlabeled_rows": len(snapshots), "targets_filled": False}


def prepare_manifest(
    exports: list[tuple[Path, str]],
    annotations: Path,
    annotations_sha256: str,
    media: list[Path],
    output: Path,
    *,
    population: tuple[Path, str] | None = None,
) -> dict[str, Any]:
    snapshots = read_snapshots(exports)
    annotation_text = _read_verified(annotations, annotations_sha256)
    source_paths = {file_sha256(path): path.expanduser().resolve() for path in media}
    rows, feature_artifacts = [], {}
    reader = csv.DictReader(io.StringIO(annotation_text))
    if not set(TARGET_COLUMNS[:11]) <= set(reader.fieldnames or []):
        raise DataError("Use the exported target CSV columns for actual staff annotations")
    for human in reader:
        key = human["task"], human["canonical_play_id"], human["feature_evidence_sha256"]
        packet = snapshots.get(key)
        if packet is None:
            raise DataError("Human target does not match the exact exported feature snapshot")
        if not human.get("value", "").strip():
            raise DataError("Staff must enter actual targets; blank targets never become zeros")
        if float(human["prediction_seconds"]) != packet["observation_clock"]["timestamp_seconds"]:
            raise DataError("Staff target refers to a different prediction clock")
        target = {
            "value": float(human["value"]),
            "source": human["source"],
            "reviewed": human["reviewed"].strip().casefold() == "true",
            "reviewer_id": human["reviewer_id"],
            "reviewed_at": human["reviewed_at"],
        }
        for field in ("double_labeled", "adjudicated"):
            if human.get(field, "").strip():
                target[field] = human[field].strip().casefold() == "true"
        if human.get("reviewer_ids", "").strip():
            target["reviewer_ids"] = [
                value.strip() for value in human["reviewer_ids"].split(";") if value.strip()
            ]
        row = {key: value for key, value in packet.items() if key != "feature_evidence"}
        row.update(target=target, split=human["split"])
        artifact = annotation_artifact(row)
        digest = hashlib.sha256(
            json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        row["target"]["annotation_sha256"] = digest
        rows.append(row)
        feature_artifacts[key] = packet["feature_evidence"]
    validated = validate_dataset(rows)
    if any(dict(row.lineage)["media_sha256"] not in source_paths for row in validated):
        raise DataError(
            "Supply each actual original source whose hash matches the exported snapshot"
        )
    release_population = json.loads(_read_verified(*population)) if population else None
    output = output.expanduser().resolve()
    if output.exists():
        raise DataError("Prepared dataset output already exists; evidence cannot be overwritten")
    output.mkdir(parents=True, exist_ok=False)
    evidence = [
        {"kind": "media", "path": str(path), "sha256": digest}
        for digest, path in source_paths.items()
    ]
    for index, row in enumerate(rows):
        key = (
            row["task"],
            row["lineage"]["canonical_play_id"],
            next(iter(row["features"].values()))["evidence_sha256"],
        )
        for kind, artifact in (
            ("features", snapshots[key]["feature_evidence"]),
            ("annotation", annotation_artifact(row)),
        ):
            path = output / f"{index}.{kind}.json"
            path.write_text(
                json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False),
                encoding="utf-8",
            )
            evidence.append({"kind": kind, "path": path.name, "sha256": file_sha256(path)})
    dataset_path = output / "dataset.jsonl"
    dataset_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True, allow_nan=False) for row in rows),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "dataset": {"path": dataset_path.name, "sha256": file_sha256(dataset_path)},
        "evidence": evidence,
        "preparation_inputs": [
            {"path": str(path.resolve()), "sha256": digest}
            for path, digest in [*exports, (annotations, annotations_sha256)]
        ],
    }
    if release_population is not None:
        manifest["release_population"] = release_population
        manifest["preparation_inputs"].append(
            {"path": str(population[0].resolve()), "sha256": population[1]}
        )
    path = output / "manifest.json"
    _write_json(path, manifest)
    return {
        "manifest_path": str(path),
        "manifest_sha256": file_sha256(path),
        "reviewed_rows": len(rows),
        "targets_invented": False,
        "unlabeled_export_rows_excluded": len(snapshots) - len(feature_artifacts),
    }
