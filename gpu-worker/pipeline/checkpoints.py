"""Content-addressed checkpoint identities and crash-safe local writes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pipeline.media_contract import MEDIA_GENERATOR_VERSION, OVERLAY_GENERATOR_VERSION

# Schema 6 invalidates pre-versioned derivatives, sampled-frame tracking and
# cut proposals that discarded short source intervals or skipped shot structure.
CHECKPOINT_SCHEMA = 6


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checkpoint_identity(
    source: Path,
    *,
    video_id: str,
    profile: str,
    stages: list[str],
    priority: int,
    revisions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invalidate every derived output when film, models or annotations change.

    Credentials and backend URLs never enter manifests. Hash weights, rather
    than trusting their filenames, to prevent resuming with replaced models.
    """
    prefixes = (
        "DETECT_", "DETECTION_", "BALL_", "OFFICIAL_", "MODEL_", "SEGMENTER_",
        "PIPELINE_", "ENABLE_", "POSE_", "REID_", "REGIME_", "CALIBRATION_",
        "COVERAGE_", "EXPERIMENTAL_", "PERIOD_RENDER_", "SAME_SESSION_", "HLS_",
        "DISABLE_NV",
    )
    excluded = {"PIPELINE_CHECKPOINT_ROOT", "PIPELINE_STORAGE_TRANSPORT"}
    config: dict[str, Any] = {
        key: value
        for key, value in sorted(os.environ.items())
        if key.startswith(prefixes)
        and key not in excluded
        and not any(secret in key for secret in ("TOKEN", "PASSWORD", "SECRET", "API_KEY"))
    }
    weights: dict[str, str] = {}
    for name, value in config.items():
        if name.endswith(("_PATH", "_MODEL")) or name == "MODEL_ROUTING_CONFIG":
            candidate = Path(str(value)).expanduser()
            if candidate.is_file():
                weights[name] = file_digest(candidate)
    default_detector = Path(os.environ.get("MODEL_DETECT_PATH", "yolov8n.pt")).expanduser()
    if default_detector.is_file() and "MODEL_DETECT_PATH" not in weights:
        weights["MODEL_DETECT_PATH"] = file_digest(default_detector)
    identity: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA,
        "video_id": video_id,
        "source_sha256": file_digest(source),
        "source_bytes": source.stat().st_size,
        "processing_profile": profile,
        "stages": stages,
        "priority_bucket": "same_session" if priority >= 10 else "nightly",
        "config": config,
        "model_sha256": weights,
        "media_generator_version": MEDIA_GENERATOR_VERSION,
        "overlay_generator_version": OVERLAY_GENERATOR_VERSION,
        "tracker_inputs_version": "ecc-hsv-v1",
        "staff_boundary_audit_version": "shot-reset-v1",
        "revisions": revisions or {},
    }
    identity["fingerprint"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode()
    ).hexdigest()
    return identity


def atomic_write(path: Path, data: bytes) -> None:
    """Publish complete bytes by rename, including on Windows/macOS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def envelope(payload: dict[str, Any], fingerprint: str, default: Any) -> bytes:
    serialized = json.dumps(payload, default=default, sort_keys=True).encode()
    return json.dumps(
        {
            "schema_version": CHECKPOINT_SCHEMA,
            "fingerprint": fingerprint,
            "payload_sha256": hashlib.sha256(serialized).hexdigest(),
            "payload": json.loads(serialized),
        },
        sort_keys=True,
    ).encode()


def read_envelope(data: bytes, fingerprint: str) -> dict[str, Any] | None:
    try:
        record = json.loads(data)
        if record.get("schema_version") != CHECKPOINT_SCHEMA:
            return None
        if record.get("fingerprint") != fingerprint:
            return None
        payload = record["payload"]
        if not isinstance(payload, dict):
            return None
        actual = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload if actual == record.get("payload_sha256") else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
