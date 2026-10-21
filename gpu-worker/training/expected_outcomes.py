"""Offline fit/evaluate CLI and manually queued training adapter.

python -m training.expected_outcomes fit --manifest manifest.json --manifest-sha256 HASH --output NEW_DIR
python -m training.expected_outcomes evaluate --manifest manifest.json --manifest-sha256 HASH --model checkpoint.json --model-sha256 HASH --output NEW_REPORT.json

All referenced local media/feature/annotation files must match the manifest hashes.
Output paths are exclusive-create; evidence and prior candidates are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pipeline.expected_outcomes.data import (
    MODEL_TYPES,
    DataError,
    Snapshot,
    annotation_artifact,
    feature_artifact,
    sha256,
    validate_dataset,
)
from pipeline.expected_outcomes.model import evaluate, fit, load_model, predict, save_model
from pipeline.runtime import check_control


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            check_control()
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path, expected: str) -> None:
    if file_sha256(path) != sha256(expected):
        raise DataError(f"Evidence hash mismatch: {path.name}")


def _read_verified(path: Path, expected: str) -> str:
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != sha256(expected):
        raise DataError(f"Evidence hash mismatch: {path.name}")
    return encoded.decode("utf-8")


def load_manifest(path: Path, expected_sha256: str) -> tuple[list[Snapshot], dict[str, Any]]:
    path = path.expanduser().resolve()
    manifest_text = _read_verified(path, expected_sha256)
    manifest = json.loads(manifest_text)
    try:
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
            raise DataError("Unsupported dataset manifest schema")
        dataset = manifest["dataset"]
        dataset_path = (path.parent / dataset["path"]).resolve()
        dataset_text = _read_verified(dataset_path, dataset["sha256"])
        raw = [json.loads(line) for line in dataset_text.splitlines() if line.strip()]
        rows = validate_dataset(raw)
        evidence, artifacts = {}, {}
        paths = [path, dataset_path]
        archived, media, media_sources = [], [], []
        for entry in manifest["evidence"]:
            evidence_path = (path.parent / entry["path"]).resolve()
            evidence.setdefault(entry["sha256"], set()).add(entry["kind"])
            if entry["kind"] in {"features", "annotation"}:
                content = _read_verified(evidence_path, entry["sha256"])
                artifacts[entry["sha256"]] = json.loads(content)
                archived.append(
                    {"sha256": entry["sha256"], "kind": entry["kind"], "content": content}
                )
            elif entry["kind"] == "media":
                _verified(evidence_path, entry["sha256"])
                media.append(entry["sha256"])
                media_sources.append({"path": str(evidence_path), "sha256": entry["sha256"]})
            else:
                raise DataError("Unsupported evidence artifact kind")
            paths.append(evidence_path)
        for raw_row in raw:
            feature_expected = feature_artifact(raw_row)
            for feature in raw_row["features"].values():
                artifact = artifacts.get(feature["evidence_sha256"])
                if not isinstance(artifact, dict):
                    raise DataError("Feature hash requires a typed evidence artifact")
                actual = {k: v for k, v in artifact.items() if k != "observations"}
                if json.dumps(actual, sort_keys=True) != json.dumps(
                    feature_expected, sort_keys=True
                ):
                    raise DataError(
                        "Feature artifact does not bind exact row values, units, lineage and clock"
                    )
            if json.dumps(
                artifacts.get(raw_row["target"]["annotation_sha256"]), sort_keys=True
            ) != json.dumps(annotation_artifact(raw_row), sort_keys=True):
                raise DataError(
                    "Annotation artifact does not bind exact reviewed target and attribution"
                )
        for row in rows:
            required = [
                (dict(row.lineage)["media_sha256"], "media"),
                (row.annotation_sha256, "annotation"),
            ]
            required.extend((digest, "features") for digest in row.evidence_hashes)
            if any(kind not in evidence.get(digest, set()) for digest, kind in required):
                raise DataError("Missing hash-verified media, feature or annotation evidence")
        return rows, {
            "manifest_sha256": expected_sha256,
            "dataset_sha256": dataset["sha256"],
            "evidence_paths": [str(p) for p in paths],
            "_release_inputs": {
                "manifest": manifest_text,
                "dataset": dataset_text,
                "artifacts": archived,
                "verified_media_sha256": sorted(set(media)),
                "media_sources": media_sources,
            },
        }
    except (KeyError, TypeError, AttributeError) as exc:
        raise DataError("Malformed dataset manifest") from exc


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)


def train_manifest(
    manifest_path: Path, manifest_sha256: str, output_dir: Path, *, task: str | None = None
) -> dict[str, Any]:
    rows, identity = load_manifest(manifest_path, manifest_sha256)
    output_dir = output_dir.expanduser().resolve()
    # Refusing any existing output also rejects symlinks/hardlinks to evidence inputs.
    if output_dir.exists():
        raise DataError(
            "Output directory already exists; evidence and candidates cannot be overwritten"
        )
    tasks = sorted({r.task for r in rows})
    if task is not None:
        if task not in tasks:
            raise DataError("Requested task has no reviewed dataset")
        tasks = [task]
    candidates = {}
    for name in tasks:
        check_control()
        candidate = fit(rows, name)
        candidate["dataset_identity"] = {
            k: v for k, v in identity.items() if k != "evidence_paths" and not k.startswith("_")
        }
        candidate["snapshot_sha256"] = hashlib.sha256(
            json.dumps([asdict(r) for r in rows if r.task == name], sort_keys=True).encode()
        ).hexdigest()
        candidates[name] = candidate
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(exist_ok=False)
    artifacts = {}
    for name, candidate in candidates.items():
        path = output_dir / f"{name}.checkpoint.json"
        digest = save_model(candidate, path)
        model = load_model(path, digest)
        report = evaluate(model, rows)
        report["dataset_identity"] = candidate["dataset_identity"]
        report_path = output_dir / f"{name}.evaluation.json"
        _write_json(report_path, report)
        release_path = output_dir / f"{name}.release-evidence.json"
        _write_json(
            release_path,
            {
                "schema_version": 1,
                "source": "real",
                "task": name,
                "weights_sha256": digest,
                "dataset_identity": candidate["dataset_identity"],
                "inputs": identity["_release_inputs"],
                "predictions": [
                    predict(model, row)
                    for row in rows
                    if row.task == name and row.split == "holdout"
                ],
            },
        )
        artifacts[name] = {
            "checkpoint_path": str(path),
            "report_path": str(report_path),
            "artifact_sha256": digest,
            "report_sha256": file_sha256(report_path),
            "release_evidence_path": str(release_path),
            "release_evidence_sha256": file_sha256(release_path),
            "model_type": MODEL_TYPES[name],
            "report": report,
        }
    result = {
        "trained": True,
        "promoted": False,
        "experimental": True,
        "manifest_sha256": manifest_sha256,
        "models": artifacts,
    }
    _write_json(output_dir / "result.json", result)
    return result


def evaluate_manifest(
    manifest_path: Path, manifest_sha256: str, model_path: Path, model_sha256: str, output: Path
) -> dict[str, Any]:
    rows, identity = load_manifest(manifest_path, manifest_sha256)
    model = load_model(model_path, model_sha256)
    if model.get("dataset_identity") != {
        k: v for k, v in identity.items() if k != "evidence_paths" and not k.startswith("_")
    }:
        raise DataError("Evaluation dataset hash differs from immutable fitting manifest")
    report = evaluate(model, rows)
    output = output.expanduser().resolve()
    if output.exists():
        raise DataError("Output already exists; cannot overwrite evidence or candidates")
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, report)
    return report


def train_expected_outcomes_job(inputs: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Existing manually queued ``train`` job; never auto-trains or promotes."""
    from pipeline import backend, storage

    if not all(inputs.get(key) for key in ("manifest_path", "manifest_sha256", "output_dir")):
        raise DataError(
            "Expected-outcomes training requires manifest_path, manifest_sha256 and new output_dir"
        )
    result = train_manifest(
        Path(inputs["manifest_path"]),
        inputs["manifest_sha256"],
        Path(inputs["output_dir"]),
        task=inputs.get("task"),
    )
    registered = {}
    for task, artifact in result["models"].items():
        check_control()
        digest = artifact["artifact_sha256"]
        root = f"models/expected_outcomes/{task}/{digest}"
        artifact_uri = storage.upload_file(
            Path(artifact["checkpoint_path"]), f"{root}/checkpoint.json"
        )
        report_uri = storage.upload_file(Path(artifact["report_path"]), f"{root}/evaluation.json")
        release_uri = storage.upload_file(
            Path(artifact["release_evidence_path"]), f"{root}/release-evidence.json"
        )
        version = backend.register_model_version(
            model_name=f"expected-{task}-linear-v1",
            version=digest[:16],
            model_type=MODEL_TYPES[task],
            artifact_uri=artifact_uri,
            metrics={
                **artifact["report"],
                "artifact_sha256": digest,
                "report_uri": report_uri,
                "report_sha256": artifact["report_sha256"],
                "release_evidence_uri": release_uri,
                "release_evidence_sha256": artifact["release_evidence_sha256"],
                "training_variant": "expected_outcomes",
                "job_id": job_id,
            },
            training_dataset_id=inputs.get("training_dataset_id"),
            model_version_id=str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"expected-outcomes:{task}:{digest}")
            ),
        )
        if backend.BACKEND_API_URL and version is None:
            raise RuntimeError("Candidate artifacts saved but registry publication failed")
        registered[task] = {
            "artifact_uri": artifact_uri,
            "report_uri": report_uri,
            "model_version_id": (version or {}).get("id"),
            "artifact_sha256": digest,
        }
    return {**result, "registered": registered}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for operation in ("prepare", "targets-template"):
        sub = commands.add_parser(operation)
        sub.add_argument("--snapshots", action="append", required=True, type=Path)
        sub.add_argument("--snapshots-sha256", action="append", required=True)
        sub.add_argument("--output", required=True, type=Path)
        if operation == "prepare":
            sub.add_argument("--annotations", required=True, type=Path)
            sub.add_argument("--annotations-sha256", required=True)
            sub.add_argument("--media", action="append", required=True, type=Path)
            sub.add_argument("--population", type=Path)
            sub.add_argument("--population-sha256")
    for command in ("fit", "evaluate"):
        sub = commands.add_parser(command)
        sub.add_argument("--manifest", required=True, type=Path)
        sub.add_argument("--manifest-sha256", required=True)
        sub.add_argument("--output", required=True, type=Path)
        if command == "fit":
            sub.add_argument("--task", choices=sorted(MODEL_TYPES))
        else:
            sub.add_argument("--model", required=True, type=Path)
            sub.add_argument("--model-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"prepare", "targets-template"}:
            from training.prepare_expected_outcomes import prepare_manifest, targets_template

            if len(args.snapshots) != len(args.snapshots_sha256):
                raise DataError("Every stage export requires its own SHA-256 checksum")
            exports = list(zip(args.snapshots, args.snapshots_sha256, strict=True))
            if args.command == "targets-template":
                result = targets_template(exports, args.output)
            else:
                if bool(args.population) != bool(args.population_sha256):
                    raise DataError("Reviewed population context requires its SHA-256 checksum")
                population = (args.population, args.population_sha256) if args.population else None
                result = prepare_manifest(
                    exports,
                    args.annotations,
                    args.annotations_sha256,
                    args.media,
                    args.output,
                    population=population,
                )
        elif args.command == "fit":
            result = train_manifest(
                args.manifest, args.manifest_sha256, args.output, task=args.task
            )
        else:
            result = evaluate_manifest(
                args.manifest, args.manifest_sha256, args.model, args.model_sha256, args.output
            )
    except (DataError, OSError, ValueError) as exc:
        parser.exit(2, f"Refused: {exc}\n")
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
