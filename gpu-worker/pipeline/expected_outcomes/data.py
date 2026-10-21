"""Versioned source-clock feature and human-target contracts.

Predictions use only the listed fields observed at or before the named clock.
Entire games/sessions, recordings, media and canonical plays remain disjoint.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pipeline.pressure.feature_extractor import PRESSURE_FEATURE_NAMES


class DataError(ValueError):
    """Missing, unreviewed, leaking or incompatible evidence must not train."""


@dataclass(frozen=True)
class Definition:
    task: str
    clock: str
    population: str
    features: tuple[tuple[str, str], ...]
    binary: bool
    version: str = "expected-outcomes-2"


_PRESSURE_YARDS = {
    "mean_gap_width",
    "max_gap_width",
    "mean_lb_depth",
    "min_lb_depth",
    "distance_yards",
}
DEFINITIONS = {
    "completion": Definition(
        "completion",
        "pass_release",
        "aimed_forward_passes_at_release",
        (("target_distance_to_qb", "yd"), ("nearest_defender_distance", "yd")),
        True,
    ),
    "rushing_gain": Definition(
        "rushing_gain",
        "handoff",
        "designed_rushes_at_handoff",
        (("runner_speed", "yd/s"), ("nearest_defender_distance", "yd")),
        False,
    ),
    "yac": Definition(
        "yac",
        "catch",
        "completed_forward_passes_at_catch",
        (("runner_speed", "yd/s"), ("nearest_defender_distance", "yd")),
        False,
    ),
    "pressure": Definition(
        "pressure",
        "snap",
        "pass_plays_at_snap_pressure_within_2.5s",
        tuple(
            (name, "yd" if name in _PRESSURE_YARDS else "count") for name in PRESSURE_FEATURE_NAMES
        ),
        True,
    ),
}
MODEL_TYPES = {
    "completion": "expected_completion",
    "rushing_gain": "expected_rushing_gain",
    "yac": "expected_yac",
    "pressure": "contextual_pressure",
}
SPLITS = ("train", "calibration", "holdout")


def definitions(task: str) -> Definition:
    try:
        return DEFINITIONS[task]
    except (KeyError, TypeError) as exc:
        raise DataError("Unknown expected-outcome task") from exc


def sha256(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DataError("Evidence requires a lowercase sha256 hash")
    return value


def finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise DataError("Features and targets must be finite numbers, never missing-value zeros")
    return float(value)


def nonempty_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataError("Evidence attribution and lineage require nonempty strings")
    return value


def feature_artifact(row: dict[str, Any], *, observations: Any = None) -> dict[str, Any]:
    """Hashable evidence packet excludes only its own circular hash reference."""
    body = {
        key: row[key]
        for key in (
            "schema_version",
            "task",
            "feature_version",
            "eligible_population",
            "source",
            "lineage",
            "observation_clock",
        )
    }
    body.update(
        kind="expected_outcome_features",
        features={
            name: {key: value for key, value in feature.items() if key != "evidence_sha256"}
            for name, feature in row["features"].items()
        },
    )
    if observations is not None:
        body["observations"] = observations
    if "capture_regime" in row:
        body["capture_regime"] = row["capture_regime"]
    if "subject" in row:
        body["subject"] = row["subject"]
    return body


def annotation_artifact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "expected_outcome_annotation",
        "task": row["task"],
        "lineage": row["lineage"],
        "observation_clock": row["observation_clock"],
        "target": {
            key: value for key, value in row["target"].items() if key != "annotation_sha256"
        },
    }


@dataclass(frozen=True)
class Snapshot:
    task: str
    vector: tuple[float, ...]
    timestamp: float
    lineage: tuple[tuple[str, str], ...]
    evidence_hashes: tuple[str, ...]
    feature_version: str
    population: str
    capture_regime: str = "unknown"
    split: str | None = None
    target: float | None = None
    annotation_sha256: str | None = None
    subject: tuple[tuple[str, str], ...] = ()


def validate_snapshot(row: dict[str, Any], *, require_target: bool = False) -> Snapshot:
    try:
        spec = definitions(row["task"])
        if (
            type(row["schema_version"]) is not int
            or row["schema_version"] != 1
            or row["feature_version"] != spec.version
            or row["source"] != "real"
            or row["eligible_population"] != spec.population
        ):
            raise DataError("Snapshot schema/source/feature version/population is incompatible")
        clock = row["observation_clock"]
        when = finite(clock["timestamp_seconds"])
        if when < 0 or clock["event"] != spec.clock:
            raise DataError("Wrong prediction observation clock")
        lineage = row["lineage"]
        if set(lineage) - {
            "session_id",
            "game_id",
            "recording_id",
            "canonical_play_id",
            "media_sha256",
        }:
            raise DataError("Unsupported mutable lineage fields")
        for key in ("session_id", "recording_id", "canonical_play_id", "media_sha256"):
            if not isinstance(lineage.get(key), str) or not lineage[key].strip():
                raise DataError(f"Missing immutable {key} lineage")
        sha256(lineage["media_sha256"])
        if lineage.get("game_id") is not None and (
            not isinstance(lineage["game_id"], str) or not lineage["game_id"]
        ):
            raise DataError("Invalid game lineage")
        features = row["features"]
        if set(features) != {name for name, _unit in spec.features}:
            raise DataError("Missing features or unsupported future/outcome features")
        values, hashes = [], []
        for name, unit in spec.features:
            feature = features[name]
            observed = finite(feature["observed_at_seconds"])
            if (
                feature["qualified"] is not True
                or feature["unit"] != unit
                or not 0 <= observed <= when
            ):
                raise DataError(
                    "Feature is unqualified, has wrong units or uses future observations"
                )
            value = finite(feature["value"])
            if name == "down":
                if value not in (1.0, 2.0, 3.0, 4.0):
                    raise DataError("Down must be an integer from one through four")
            elif unit == "count":
                if not value.is_integer() or (name != "rusher_blocker_diff" and value < 0):
                    raise DataError("Count features must be integral with valid sign")
            elif value < 0:
                raise DataError("Distance and speed features must be nonnegative")
            values.append(value)
            hashes.append(sha256(feature["evidence_sha256"]))
        target, annotation = None, None
        subject = row["subject"]
        if subject.get("scope") == "player":
            if set(subject) != {"scope", "role", "tracklet_id"} or subject.get("role") != {
                "completion": "QB",
                "rushing_gain": "rusher",
                "yac": "receiver",
            }.get(spec.task):
                raise DataError("Prediction actor role does not match task")
            nonempty_string(subject["tracklet_id"])
        elif (
            spec.task != "pressure"
            or subject.get("scope") != "team"
            or set(subject) != {"scope", "reason"}
        ):
            raise DataError("Prediction requires an explicit actor or team scope")
        else:
            nonempty_string(subject["reason"])
        split = row.get("split")
        if require_target:
            truth = row["target"]
            nonempty_string(truth["reviewer_id"])
            try:
                reviewed = datetime.fromisoformat(
                    nonempty_string(truth["reviewed_at"]).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise DataError("Review time must be a valid ISO timestamp") from exc
            if reviewed.tzinfo is None:
                raise DataError("Review timestamp must include timezone")
            if (
                truth["reviewed"] is not True
                or split not in SPLITS
                or truth.get("source") != "human"
            ):
                raise DataError(
                    "Only attributed reviewed human targets in explicit splits may train"
                )
            target = finite(truth["value"])
            if spec.binary and target not in (0.0, 1.0):
                raise DataError("Binary targets must be zero or one")
            annotation = sha256(truth["annotation_sha256"])
        return Snapshot(
            spec.task,
            tuple(values),
            when,
            tuple(sorted((k, v) for k, v in lineage.items() if v is not None)),
            tuple(hashes),
            spec.version,
            spec.population,
            row.get("capture_regime", "unknown"),
            split,
            target,
            annotation,
            tuple(sorted(subject.items())),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise DataError("Missing or malformed evidence snapshot") from exc


def validate_dataset(rows: list[dict[str, Any]]) -> list[Snapshot]:
    if not rows:
        raise DataError("Empty dataset: actual reviewed targets are required")
    validated = [validate_snapshot(row, require_target=True) for row in rows]
    groups: dict[tuple[str, str], str] = {}
    canonical = set()
    for row in validated:
        for key, value in row.lineage:
            previous = groups.setdefault((key, value), row.split)
            if previous != row.split:
                raise DataError(f"{key} leakage across train/calibration/holdout")
        identity = row.task, dict(row.lineage)["canonical_play_id"]
        if identity in canonical:
            raise DataError("Duplicate canonical play/alternate view in task population")
        canonical.add(identity)
    for task in {row.task for row in validated}:
        for split in SPLITS:
            subset = [row for row in validated if row.task == task and row.split == split]
            if len(subset) < 2:
                raise DataError(f"{task}/{split} requires at least two reviewed canonical plays")
            if definitions(task).binary and {row.target for row in subset} != {0.0, 1.0}:
                raise DataError(f"{task}/{split} requires both reviewed classes")
    return sorted(validated, key=lambda row: (row.task, row.split, row.lineage))
