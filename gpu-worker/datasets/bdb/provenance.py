"""Provenance + artifact writing for the BDB adapter (Issue #164).

Every normalized artifact set ships with a ``manifest.json`` that records where
the data came from, which competition/season, source-file checksums, the license
note, and — loudly — that the data is offline pretraining/evaluation only. This
is the contract downstream offline work (#139/#140/#141/#150) reads before
touching a single record, so BDB data can never be silently treated as Toledo
film or as a calibrated tracking result.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from datasets.bdb.schema import (
    COORDINATE_FRAME,
    FIELD_MAPPING,
    SCHEMA_VERSION,
    USAGE_MARKER,
    NormalizedDataset,
)

# License / terms note recorded into every manifest. BDB is governed by the
# per-competition Kaggle rules; this is a caveat, not legal advice.
BDB_LICENSE_NOTE = (
    "NFL Big Data Bowl data is distributed via Kaggle under each competition's "
    "rules. Typically usable for the competition and non-commercial research; "
    "redistribution is generally NOT permitted. Verify the specific "
    "competition's rules before any use beyond offline research. Raw Kaggle data "
    "is never committed to this repository."
)

# Recorded so a consumer never mistakes BDB field-yard coordinates for a
# calibrated Football-IQ single-camera tracking output.
COORDINATE_NOTE = (
    "Coordinates are BDB ground-truth field yards (x: 0-120 end zone to end "
    "zone, y: 0-53.3 sideline to sideline), NOT pixel space and NOT a calibrated "
    "Football-IQ homography output. Football-IQ derives field coordinates from "
    "single-camera video via #127/#128/#129; BDB coordinates are an analogue for "
    "offline pretraining/evaluation only and are not interchangeable with Toledo "
    "tracking."
)

LABEL_NOTE = (
    "BDB labels (offense_formation, route_ran, events, etc.) are NFL labels. They "
    "are NOT asserted to equal Toledo labels and must be validated on Toledo "
    "footage before any coach-facing use."
)


@dataclass
class SourceFile:
    """One raw BDB CSV that fed the normalization, with an integrity checksum."""

    name: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Provenance:
    """Full provenance record written to ``manifest.json``."""

    competition: str
    season: int | None
    schema_version: int = SCHEMA_VERSION
    usage: str = USAGE_MARKER
    coordinate_frame: str = COORDINATE_FRAME
    generated_at: str = ""
    license_note: str = BDB_LICENSE_NOTE
    coordinate_note: str = COORDINATE_NOTE
    label_note: str = LABEL_NOTE
    source_files: list[SourceFile] = field(default_factory=list)
    record_counts: dict[str, int] = field(default_factory=dict)
    field_mapping: list[dict[str, str]] = field(default_factory=lambda: list(FIELD_MAPPING))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


def _sha256_file(path: str, *, chunk: int = 1 << 20) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def collect_source_files(input_dir: str) -> list[SourceFile]:
    """Checksum every ``*.csv`` in ``input_dir`` for the provenance record."""
    out: list[SourceFile] = []
    for name in sorted(os.listdir(input_dir)):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(input_dir, name)
        if not os.path.isfile(path):
            continue
        sha, size = _sha256_file(path)
        out.append(SourceFile(name=name, sha256=sha, size_bytes=size))
    return out


def build_provenance(
    *,
    competition: str,
    season: int | None,
    input_dir: str | None,
    dataset: NormalizedDataset,
) -> Provenance:
    prov = Provenance(competition=competition, season=season)
    prov.generated_at = datetime.now(UTC).isoformat()
    prov.record_counts = dataset.record_counts()
    if input_dir is not None and os.path.isdir(input_dir):
        prov.source_files = collect_source_files(input_dir)
    return prov


# ── artifact writing ─────────────────────────────────────────────────────────


def _write_jsonl(path: str, records: list[Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec.to_dict(), separators=(",", ":")))
            handle.write("\n")


def write_artifacts(
    dataset: NormalizedDataset,
    provenance: Provenance,
    output_dir: str,
) -> dict[str, str]:
    """Write normalized JSONL tables + ``manifest.json`` to ``output_dir``.

    Returns a map of artifact name → absolute path. The output dir is expected
    to be a gitignored local/cache path; raw Kaggle data is never written here.
    """
    os.makedirs(output_dir, exist_ok=True)
    written: dict[str, str] = {}

    tables = {
        "games": dataset.games,
        "plays": dataset.plays,
        "players": dataset.players,
        "player_plays": dataset.player_plays,
        "tracking": dataset.tracking,
    }
    for name, records in tables.items():
        path = os.path.join(output_dir, f"{name}.jsonl")
        _write_jsonl(path, records)
        written[name] = os.path.abspath(path)

    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(provenance.to_dict(), handle, indent=2)
        handle.write("\n")
    written["manifest"] = os.path.abspath(manifest_path)

    return written
