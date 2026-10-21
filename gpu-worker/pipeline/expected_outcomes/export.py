"""Hash-bound staff handoff of actual normal-stage feature snapshots, without targets."""

from __future__ import annotations

import hashlib
import json

from pipeline.expected_outcomes.data import DataError, feature_artifact, validate_snapshot


def export_feature_snapshots(
    clip_artifacts,
    *,
    video_id,
    job_id,
    source_uri,
    media_sha256,
    source_clock_verified,
    clip_revisions,
):
    from pipeline import storage

    if source_clock_verified is not True:
        return None
    rows, scopes = {}, {}
    for clip_id, artifacts in clip_artifacts.items():
        for task, packet in (
            (artifacts.get("expected_outcomes") or {}).get("feature_snapshots", {}).items()
        ):
            snapshot = validate_snapshot(packet)
            scope = clip_revisions.get(str(clip_id))
            if (
                not isinstance(scope, dict)
                or set(scope) != {"annotation_revision", "observation_revision"}
                or any(type(value) is not int or value < 0 for value in scope.values())
            ):
                raise DataError("Export requires the source clip's exact review revisions")
            scopes[str(clip_id)] = scope
            lineage = dict(snapshot.lineage)
            if (
                task != snapshot.task
                or lineage["recording_id"] != video_id
                or lineage["media_sha256"] != media_sha256
            ):
                raise DataError("Export feature snapshot differs from the actual source recording")
            evidence = packet.get("feature_evidence")
            encoded = json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            if evidence is None or set(snapshot.evidence_hashes) != {
                hashlib.sha256(encoded).hexdigest()
            }:
                raise DataError("Export requires the unchanged actual typed feature evidence")
            expected = feature_artifact(packet)
            if {key: value for key, value in evidence.items() if key != "observations"} != expected:
                raise DataError("Export feature evidence does not bind the exact snapshot")
            rows[f"{clip_id}:{task}"] = {
                key: packet[key]
                for key in (
                    "schema_version",
                    "task",
                    "feature_version",
                    "eligible_population",
                    "source",
                    "capture_regime",
                    "lineage",
                    "observation_clock",
                    "features",
                    "subject",
                    "feature_evidence",
                )
                if key in packet
            }
    if not rows:
        return None
    document = {
        "schema_version": 1,
        "kind": "expected_outcome_feature_export",
        "source": "real",
        "video_id": video_id,
        "job_id": job_id,
        "source_uri": source_uri,
        "media_sha256": media_sha256,
        "source_clock": "verified_native_pts",
        "feature_snapshots": rows,
        "clip_revisions": scopes,
        "targets_included": False,
        "review_attestations_filled": False,
    }
    data = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > 64 * 1024 * 1024:
        raise DataError("Expected feature export exceeds the bounded staff artifact limit")
    digest = hashlib.sha256(data).hexdigest()
    uri = storage.upload_bytes(
        data,
        f"training_features/expected_outcomes/{video_id}/{digest}.json",
        content_type="application/json",
    )
    return {
        "artifact_uri": uri,
        "sha256": digest,
        "row_count": len(rows),
        "size_bytes": len(data),
        "video_id": video_id,
        "job_id": job_id,
        "media_sha256": media_sha256,
        "source_clock": "verified_native_pts",
    }
