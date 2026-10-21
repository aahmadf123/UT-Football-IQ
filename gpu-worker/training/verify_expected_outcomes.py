"""Separate frozen-hash evaluation job: reload weights, never fit or copy targets."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any

from pipeline.expected_outcomes.data import (
    DataError,
    annotation_artifact,
    feature_artifact,
    validate_dataset,
    validate_snapshot,
)
from pipeline.expected_outcomes.model import _snapshot_hash, evaluate, load_model, predict
from pipeline.runtime import check_control, lease_payload
from pipeline.video_timeline import VideoTimeline
from training.expected_outcomes import _read_verified, _verified

VERIFIER_VERSION = "expected-outcomes-independent-verification-2"


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    if (
        isinstance(a, (int, float))
        and not isinstance(a, bool)
        and isinstance(b, (int, float))
        and not isinstance(b, bool)
    ):
        return math.isclose(a, b, abs_tol=1e-10, rel_tol=1e-8)
    return type(a) is type(b) and a == b


def verify_local(
    model_path: Path,
    model_hash: str,
    report_path: Path,
    report_hash: str,
    audit_path: Path,
    audit_hash: str,
) -> dict[str, Any]:
    """Reconstruct each heldout feature-only input and independently run real weights."""
    model = load_model(model_path, model_hash)
    report = json.loads(_read_verified(report_path, report_hash))
    audit = json.loads(_read_verified(audit_path, audit_hash))
    try:
        if (
            audit["schema_version"] != 1
            or audit["source"] != "real"
            or audit["task"] != model["task"]
            or audit["weights_sha256"] != model_hash
            or audit["dataset_identity"] != model["dataset_identity"]
        ):
            raise DataError("Verification candidate/manifest binding differs")
        inputs, identity = audit["inputs"], audit["dataset_identity"]
        media = inputs["media_sources"]
        if {source["sha256"] for source in media} != set(inputs["verified_media_sha256"]):
            raise DataError("Independent verification requires original media references")
        timelines = {}
        for source in media:
            source_path = Path(source["path"]).expanduser().resolve()
            before = source_path.stat()
            _verified(source_path, source["sha256"])
            timeline = VideoTimeline.from_path(source_path)
            after = source_path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_dev,
                after.st_ino,
            ):
                raise DataError("Original media changed during hash/native-clock verification")
            if timeline.verified is not True or not timeline.timestamps:
                raise DataError("Original media lacks a verified native source PTS timeline")
            timelines[source["sha256"]] = timeline
        manifest_text, dataset_text = inputs["manifest"], inputs["dataset"]
        if (
            hashlib.sha256(manifest_text.encode()).hexdigest() != identity["manifest_sha256"]
            or hashlib.sha256(dataset_text.encode()).hexdigest() != identity["dataset_sha256"]
            or json.loads(manifest_text)["dataset"]["sha256"] != identity["dataset_sha256"]
        ):
            raise DataError("Verification manifest or dataset hash differs")
        raw = [json.loads(line) for line in dataset_text.splitlines() if line.strip()]
        rows = validate_dataset(raw)
        for row in rows:
            timeline = timelines.get(dict(row.lineage)["media_sha256"])
            if (
                timeline is None
                or abs(timeline.timestamp(timeline.frame_for_time(row.timestamp)) - row.timestamp)
                > 1e-6
            ):
                raise DataError("Prediction clock is not an actual original-media native PTS")
        artifacts = {}
        for artifact in inputs["artifacts"]:
            if hashlib.sha256(artifact["content"].encode()).hexdigest() != artifact["sha256"]:
                raise DataError("Verification evidence hash differs")
            artifacts[(artifact["kind"], artifact["sha256"])] = json.loads(artifact["content"])
        for row in raw:
            expected = feature_artifact(row)
            for feature in row["features"].values():
                actual = artifacts[("features", feature["evidence_sha256"])]
                if not _close({k: v for k, v in actual.items() if k != "observations"}, expected):
                    raise DataError("Verification feature artifact differs")
            if not _close(
                artifacts[("annotation", row["target"]["annotation_sha256"])],
                annotation_artifact(row),
            ):
                raise DataError("Verification reviewed target differs")
        for split in ("train", "calibration", "holdout"):
            if (
                sorted(
                    _snapshot_hash(row)
                    for row in rows
                    if row.task == model["task"] and row.split == split
                )
                != model["split_snapshot_hashes"][split]
            ):
                raise DataError("Verification immutable fitted/heldout snapshots differ")
        supplied = {p["lineage"]["canonical_play_id"]: p for p in audit["predictions"]}
        if len(supplied) != len(audit["predictions"]):
            raise DataError("Duplicate supplied verification predictions")
        recomputed = []
        for packet in raw:
            if packet["task"] != model["task"] or packet["split"] != "holdout":
                continue
            check_control()
            # Human targets/splits never reach forward prediction. They are used only by evaluate below.
            feature_only = {
                key: value for key, value in packet.items() if key not in {"target", "split"}
            }
            output = predict(model, validate_snapshot(feature_only))
            provided = supplied.get(output["lineage"]["canonical_play_id"])
            if not _close(output, provided):
                raise DataError("Submitted prediction differs from independent forward output")
            recomputed.append(output)
        if len(recomputed) != len(supplied):
            raise DataError("Verification must cover the complete immutable holdout")
        actual_report = evaluate(model, rows)
        for key in (
            "task",
            "split",
            "weights_sha256",
            "eligible_population",
            "count",
            "metrics",
            "baseline",
            "group_counts",
        ):
            if not _close(actual_report[key], report[key]):
                raise DataError("Submitted report differs from independent forward evaluation")
        return {
            "verifier_version": VERIFIER_VERSION,
            "forward_outputs_matched": True,
            "task": model["task"],
            "artifact_sha256": model_hash,
            "report_sha256": report_hash,
            "release_evidence_sha256": audit_hash,
            "dataset_identity": identity,
            "prediction_count": len(recomputed),
            "feature_version": model["feature_version"],
            "source_media_verified": True,
            "source_clock_verified": True,
            "recomputed_predictions_sha256": hashlib.sha256(
                json.dumps(recomputed, sort_keys=True, allow_nan=False).encode()
            ).hexdigest(),
        }
    except (KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        raise DataError(f"Independent verification refused: {exc}") from exc


def verify_expected_outcomes_job(inputs: dict[str, Any], job_id: str) -> dict[str, Any]:
    from pipeline import storage

    lease = lease_payload(job_id)
    if not all(lease.get(key) for key in ("worker_id", "lease_token", "attempt_count")):
        raise DataError("Independent release verification requires an active fenced worker job")
    paths = []
    try:
        for kind in ("artifact", "report", "release_evidence"):
            paths.append(storage.download_to_temp(inputs[f"{kind}_uri"]))
        result = verify_local(
            paths[0],
            inputs["artifact_sha256"],
            paths[1],
            inputs["report_sha256"],
            paths[2],
            inputs["release_evidence_sha256"],
        )
        if (
            result["task"] != inputs["task"]
            or result["dataset_identity"] != inputs["dataset_identity"]
        ):
            raise DataError("Verification differs from frozen API challenge")
        receipt = {
            **result,
            "schema_version": 1,
            "kind": "expected_outcome_verification_receipt",
            "challenge_id": inputs["challenge_id"],
            "verification_job_id": job_id,
            "model_id": inputs["model_id"],
            "worker_id": lease["worker_id"],
            "attempt_count": lease["attempt_count"],
        }
        with tempfile.TemporaryDirectory(prefix="expected-outcome-verification-") as directory:
            path = Path(directory) / "receipt.json"
            encoded = json.dumps(receipt, sort_keys=True, allow_nan=False).encode()
            path.write_bytes(encoded)
            digest = hashlib.sha256(encoded).hexdigest()
            uri = storage.upload_file(
                path,
                f"models/expected_outcomes/verification/{inputs['challenge_id']}/{digest}.json",
            )
        return {
            "verification_receipt_uri": uri,
            "verification_receipt_sha256": digest,
            "challenge_id": inputs["challenge_id"],
            "verifier_version": VERIFIER_VERSION,
            "forward_outputs_matched": True,
        }
    finally:
        for path in paths:
            Path(path).unlink(missing_ok=True)
