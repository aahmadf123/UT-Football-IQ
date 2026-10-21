"""Bind locally read full media bytes to the API's upload registration."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

FULL_HASH_BYTES = 64 * 1024 * 1024
EDGE_BYTES = 4 * 1024 * 1024
SAMPLE_BYTES = 1024 * 1024


def source_stat(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def content_identity(path: Path) -> dict[str, Any]:
    before = source_stat(path)
    size = before[2]
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
        full = digest.hexdigest()
        registration = full
        if size > FULL_HASH_BYTES:
            sampled = hashlib.sha256(b"football-iq sampled fingerprint v1:")
            sampled.update(str(size).encode())
            span = size - 2 * EDGE_BYTES - SAMPLE_BYTES
            ranges = [(0, EDGE_BYTES), (size - EDGE_BYTES, size)] + [
                (
                    EDGE_BYTES + span * (index + 1) // 17,
                    EDGE_BYTES + span * (index + 1) // 17 + SAMPLE_BYTES,
                )
                for index in range(16)
            ]
            for start, end in sorted(ranges):
                source.seek(start)
                block = source.read(end - start)
                if len(block) != end - start:
                    raise ValueError("Source changed while computing content identity")
                sampled.update(start.to_bytes(8, "big"))
                sampled.update(block)
            registration = sampled.hexdigest()
    if source_stat(path) != before:
        raise ValueError("Source changed while computing content identity")
    return {
        "source_sha256": full,
        "source_bytes": size,
        "source_registration_fingerprint": registration,
        "source_stat": before,
    }


def registered_source_receipt(
    path: Path,
    identity: dict[str, Any],
    video_id: str,
    input_uri: str,
    registration: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not registration:
        return None
    # Legacy unregistered sources remain playable but grant no full identity.
    if registration.get("content_sha256") is None or registration.get("size_bytes") is None:
        return None
    if (
        type(registration.get("size_bytes")) is not int
        or registration["size_bytes"] <= 0
        or registration["size_bytes"] != identity["source_bytes"]
        or registration.get("content_sha256") != identity["source_registration_fingerprint"]
        or registration.get("storage_uri") != input_uri
        or source_stat(path) != identity["source_stat"]
    ):
        raise ValueError("Processed media does not match the registered source")
    return {
        "schema_version": 1,
        "video_id": video_id,
        "storage_uri": input_uri,
        "size_bytes": identity["source_bytes"],
        "registration_fingerprint": identity["source_registration_fingerprint"],
        "registration_algorithm": "full-sha256"
        if identity["source_bytes"] <= FULL_HASH_BYTES
        else "sampled-sha256-v1",
        "full_sha256": identity["source_sha256"],
    }
