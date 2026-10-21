"""Independent expected-outcome release review; the API never runs model weights.

The worker supplies immutable reviewed snapshots, typed original label/feature
artifacts and complete held-out predictions. This module independently checks
their hashes, clocks, group splits and losses before the existing human promotion.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from anyio import to_thread
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    JobStatus,
    JobType,
    ModelVersion,
    PipelineMode,
    ProcessingJob,
    SystemSetting,
    User,
    WorkerRuntime,
)
from app.services.metric_definitions import DEFINITIONS
from app.storage import (
    ARTIFACTS,
    get_s3_client,
    is_valid_key,
    local_object_path,
    parse_storage_uri,
    physical_bucket,
)

TASK_TYPES = {
    "completion": "expected_completion",
    "rushing_gain": "expected_rushing_gain",
    "yac": "expected_yac",
    "pressure": "contextual_pressure",
}
FEATURE_VERSION = "expected-outcomes-2"
FEATURES = {
    "completion": (("target_distance_to_qb", "yd"), ("nearest_defender_distance", "yd")),
    "rushing_gain": (("runner_speed", "yd/s"), ("nearest_defender_distance", "yd")),
    "yac": (("runner_speed", "yd/s"), ("nearest_defender_distance", "yd")),
    "pressure": tuple(
        (
            name,
            "yd"
            if name
            in {
                "mean_gap_width",
                "max_gap_width",
                "mean_lb_depth",
                "min_lb_depth",
                "distance_yards",
            }
            else "count",
        )
        for name in (
            "mean_gap_width",
            "max_gap_width",
            "dl_count",
            "box_count",
            "rusher_blocker_diff",
            "lb_count",
            "mean_lb_depth",
            "min_lb_depth",
            "blitzers",
            "dl_in_a_gap",
            "dl_in_b_gap",
            "dl_in_c_gap",
            "dl_on_edge",
            "down",
            "distance_yards",
        )
    ),
}
CLOCKS = {
    "completion": "pass_release",
    "rushing_gain": "handoff",
    "yac": "catch",
    "pressure": "snap",
}
POPULATIONS = {
    "completion": "aimed_forward_passes_at_release",
    "rushing_gain": "designed_rushes_at_handoff",
    "yac": "completed_forward_passes_at_catch",
    "pressure": "pass_plays_at_snap_pressure_within_2.5s",
}
MIN_COUNTS = {"train": 30, "calibration": 10, "holdout": 20}
MAX_ECE = 0.1
KNOWN_REGIMES = {"fixed_sideline", "fixed_endzone", "drone_follow", "unconstrained"}
VERIFIER_VERSION = "expected-outcomes-independent-verification-2"


def _population(audit: dict[str, Any], groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Page-19 population is a separate gate from fitting a small candidate."""
    manifest = json.loads(audit["inputs"]["manifest"])
    reviewed = (manifest.get("release_population") or {}).get("source_groups", [])
    confirmed = {}
    memberships: dict[tuple[str, str], str] = {}
    for group in reviewed:
        reviewers = group.get("reviewer_ids")
        if (
            group.get("independence_confirmed") is True
            and isinstance(reviewers, list)
            and reviewers
            and all(isinstance(r, str) and r.strip() for r in reviewers)
            and group.get("category") in {"practice", "coach_game", "television"}
        ):
            identity = _text(group.get("group_id"))
            if identity in confirmed:
                raise ValueError("Duplicate confirmed independent source group")
            for field in ("recording_ids", "media_sha256"):
                values = group.get(field)
                if (
                    not isinstance(values, list)
                    or not values
                    or any(not isinstance(v, str) or not v.strip() for v in values)
                ):
                    raise ValueError(
                        "Confirmed source groups need typed nonempty source memberships"
                    )
                for value in values:
                    if field == "media_sha256":
                        _hash(value)
                    if memberships.setdefault((field, value), identity) != identity:
                        raise ValueError(
                            "One actual recording/media cannot count as independent groups"
                        )
            confirmed[identity] = group
    category_plays: dict[str, list[dict[str, Any]]] = {
        category: [] for category in ("practice", "coach_game", "television")
    }
    category_groups: dict[str, set[str]] = {category: set() for category in category_plays}
    missing = []
    double = 0
    for row in groups["holdout"]:
        lineage = row["lineage"]
        identity = lineage.get("game_id") or lineage["session_id"]
        group = confirmed.get(identity)
        if (
            group is None
            or lineage["recording_id"] not in group.get("recording_ids", [])
            or lineage["media_sha256"] not in group.get("media_sha256", [])
        ):
            missing.append(identity)
            continue
        category = group["category"]
        category_plays[category].append(row)
        category_groups[category].add(identity)
        target = row["target"]
        ids = target.get("reviewer_ids")
        if (
            target.get("double_labeled") is True
            and target.get("adjudicated") is True
            and isinstance(ids, list)
            and len(set(ids)) >= 2
            and all(isinstance(r, str) and r.strip() for r in ids)
            and target["reviewer_id"] in ids
        ):
            double += 1
    reasons = []
    if missing:
        reasons.append("independently_confirmed_source_groups_missing")
    if len(groups["holdout"]) < 240:
        reasons.append("requires_240_distinct_heldout_canonical_plays")
    if len(set().union(*category_groups.values())) < 12:
        reasons.append("requires_12_independent_heldout_groups")
    for category in category_plays:
        if len(category_plays[category]) < 80 or len(category_groups[category]) < 4:
            reasons.append(f"requires_80_plays_and_four_groups_{category}")
    if double < math.ceil(len(groups["holdout"]) * 0.2):
        reasons.append("requires_20_percent_double_labeled_adjudicated_outcomes")
    return {
        "passed": not reasons,
        "missing_data_reasons": reasons,
        "canonical_plays": len(groups["holdout"]),
        "independent_groups": len(set().union(*category_groups.values())),
        "plays_by_category": {key: len(rows) for key, rows in category_plays.items()},
        "groups_by_category": {key: len(values) for key, values in category_groups.items()},
        "double_labeled_adjudicated_plays": double,
    }


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Release evidence requires a SHA-256 checksum")
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Release evidence requires finite numerical values")
    return float(value)


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Release evidence attribution and lineage must be named")
    return value


def _digest_text(text: str, expected: str) -> str:
    if hashlib.sha256(text.encode()).hexdigest() != _hash(expected):
        raise ValueError("Release evidence checksum mismatch")
    return text


def _read(uri: str, expected: str | None = None, *, parse: bool = True) -> Any:
    if not isinstance(uri, str):
        raise ValueError("Release artifact URI must be a storage reference")
    scheme, bucket, key = parse_storage_uri(uri)
    if bucket != ARTIFACTS or not is_valid_key(key):
        raise ValueError("Release artifacts must use the owned artifacts namespace")
    stream = None
    try:
        if scheme == "local":
            stream = local_object_path(bucket, key).open("rb")
        else:
            from botocore.exceptions import BotoCoreError, ClientError

            try:
                response = get_s3_client().get_object(Bucket=physical_bucket(bucket), Key=key)
            except (BotoCoreError, ClientError) as exc:
                raise ValueError(
                    "Release object is unavailable in the owned artifact store"
                ) from exc
            stream = response["Body"]
        chunks, size, hasher = [], 0, hashlib.sha256()
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > 64 * 1024 * 1024:
                raise ValueError("Release evidence exceeds the evaluation size limit")
            hasher.update(block)
            if parse:
                chunks.append(block)
        digest = hasher.hexdigest()
        encoded = b"".join(chunks)
    finally:
        if stream is not None:
            stream.close()
    if expected is not None and digest != _hash(expected):
        raise ValueError("Registered release artifact checksum mismatch")
    return json.loads(encoded) if parse else digest


def _same(a: Any, b: Any) -> bool:
    """Type-sensitive equality so booleans cannot impersonate numeric evidence."""
    return json.dumps(a, sort_keys=True, allow_nan=False) == json.dumps(
        b, sort_keys=True, allow_nan=False
    )


def _row(row: dict[str, Any]) -> None:
    task = row["task"]
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["source"] != "real"
        or row["feature_version"] != FEATURE_VERSION
        or row["eligible_population"] != POPULATIONS[task]
        or row["split"] not in MIN_COUNTS
    ):
        raise ValueError("Incompatible real reviewed release snapshot")
    subject = row["subject"]
    if task == "pressure":
        if subject.get("scope") != "team" or set(subject) != {"scope", "reason"}:
            raise ValueError("Contextual pressure requires explicit team scope")
        _text(subject["reason"])
    elif (
        subject.get("scope") != "player"
        or set(subject) != {"scope", "role", "tracklet_id"}
        or subject.get("role")
        != {"completion": "QB", "rushing_gain": "rusher", "yac": "receiver"}[task]
    ):
        raise ValueError("Player prediction requires the resolved task actor")
    else:
        _text(subject["tracklet_id"])
    lineage = row["lineage"]
    if set(lineage) - {
        "session_id",
        "game_id",
        "recording_id",
        "canonical_play_id",
        "media_sha256",
    }:
        raise ValueError("Unsupported release lineage")
    for field in ("session_id", "recording_id", "canonical_play_id"):
        _text(lineage[field])
    for value in lineage.values():
        if value is not None:
            _text(value)
    _hash(lineage["media_sha256"])
    when = _number(row["observation_clock"]["timestamp_seconds"])
    if when < 0 or row["observation_clock"]["event"] != CLOCKS[task]:
        raise ValueError("Wrong release prediction-time clock")
    if set(row["features"]) != {name for name, _unit in FEATURES[task]}:
        raise ValueError("Missing or future outcome input features")
    for name, unit in FEATURES[task]:
        feature = row["features"][name]
        value = _number(feature["value"])
        if (
            feature["qualified"] is not True
            or feature["unit"] != unit
            or not 0 <= _number(feature["observed_at_seconds"]) <= when
        ):
            raise ValueError("Unqualified units or future features in release evidence")
        if name == "down" and value not in (1.0, 2.0, 3.0, 4.0):
            raise ValueError("Invalid down")
        if unit == "count" and (
            not value.is_integer() or (name != "rusher_blocker_diff" and value < 0)
        ):
            raise ValueError("Invalid count feature")
        if unit != "count" and value < 0:
            raise ValueError("Negative speed or distance feature")
        _hash(feature["evidence_sha256"])
    target = row["target"]
    if target["source"] != "human" or target["reviewed"] is not True:
        raise ValueError("Only reviewed human targets may support release")
    _text(target["reviewer_id"])
    reviewed = datetime.fromisoformat(_text(target["reviewed_at"]).replace("Z", "+00:00"))
    if reviewed.tzinfo is None:
        raise ValueError("Review time requires a timezone")
    truth = _number(target["value"])
    if task in {"completion", "pressure"} and truth not in (0.0, 1.0):
        raise ValueError("Invalid binary target")
    _hash(target["annotation_sha256"])


def _validated_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    inputs, identity = audit["inputs"], audit["dataset_identity"]
    manifest = json.loads(_digest_text(inputs["manifest"], identity["manifest_sha256"]))
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("Wrong release manifest schema")
    dataset = _digest_text(inputs["dataset"], identity["dataset_sha256"])
    if manifest["dataset"]["sha256"] != identity["dataset_sha256"]:
        raise ValueError("Release manifest and dataset differ")
    rows = [json.loads(line) for line in dataset.splitlines() if line.strip()]
    if not rows:
        raise ValueError("No reviewed release data")
    artifacts = {}
    listed = {(entry["kind"], entry["sha256"]) for entry in manifest["evidence"]}
    verified_media = {_hash(value) for value in inputs["verified_media_sha256"]}
    if {source["sha256"] for source in inputs["media_sources"]} != verified_media or any(
        not isinstance(source["path"], str) or not source["path"].strip()
        for source in inputs["media_sources"]
    ):
        raise ValueError("Original media references are missing from frozen verification inputs")
    if verified_media != {digest for kind, digest in listed if kind == "media"}:
        raise ValueError("Original media verification evidence is missing")
    for entry in inputs["artifacts"]:
        if (entry["kind"], entry["sha256"]) not in listed or entry["kind"] not in {
            "features",
            "annotation",
        }:
            raise ValueError("Unbound release evidence artifact")
        artifacts[(entry["kind"], entry["sha256"])] = json.loads(
            _digest_text(entry["content"], entry["sha256"])
        )
    groups: dict[tuple[str, str], str] = {}
    plays: set[tuple[str, str]] = set()
    for row in rows:
        _row(row)
        lineage = row["lineage"]
        if lineage["media_sha256"] not in verified_media:
            raise ValueError("Original media hash missing from release evidence")
        for key, value in lineage.items():
            if value is not None and groups.setdefault((key, value), row["split"]) != row["split"]:
                raise ValueError("Session/game/recording/media/canonical split leakage")
        play = row["task"], lineage["canonical_play_id"]
        if play in plays:
            raise ValueError("Duplicate canonical play in release population")
        plays.add(play)
        feature = {
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
        feature.update(
            kind="expected_outcome_features",
            features={
                name: {k: v for k, v in value.items() if k != "evidence_sha256"}
                for name, value in row["features"].items()
            },
        )
        if "capture_regime" in row:
            feature["capture_regime"] = row["capture_regime"]
        if "subject" in row:
            feature["subject"] = row["subject"]
        for value in row["features"].values():
            actual = artifacts[("features", value["evidence_sha256"])]
            if not _same({k: v for k, v in actual.items() if k != "observations"}, feature):
                raise ValueError("Feature artifact does not bind exact release values and clock")
        annotation = {
            "schema_version": 1,
            "kind": "expected_outcome_annotation",
            "task": row["task"],
            "lineage": lineage,
            "observation_clock": row["observation_clock"],
            "target": {k: v for k, v in row["target"].items() if k != "annotation_sha256"},
        }
        if not _same(artifacts[("annotation", row["target"]["annotation_sha256"])], annotation):
            raise ValueError("Annotation does not bind exact reviewed release target")
    return rows


def _quantile(values: list[float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (position - lower) * (values[upper] - values[lower])


def _metrics(y: list[float], prediction: list[float], binary: bool) -> dict[str, Any]:
    from app.services.model_diagnostics import outcome_diagnostics

    n = len(y)
    if not n or n != len(prediction):
        raise ValueError("Missing heldout predictions")
    if binary:
        prediction = [min(1 - 1e-12, max(1e-12, p)) for p in prediction]
        bins = []
        for index in range(10):
            ids = [i for i, p in enumerate(prediction) if min(int(p * 10), 9) == index]
            if ids:
                bins.append(
                    {
                        "lower": index / 10,
                        "upper": index / 10 + 0.1,
                        "count": len(ids),
                        "predicted_mean": sum(prediction[i] for i in ids) / len(ids),
                        "observed_mean": sum(y[i] for i in ids) / len(ids),
                    }
                )
        return {
            "brier": sum((p - t) ** 2 for p, t in zip(prediction, y, strict=True)) / n,
            **outcome_diagnostics(y, prediction, binary),
            "log_loss": -sum(
                t * math.log(p) + (1 - t) * math.log(1 - p)
                for p, t in zip(prediction, y, strict=True)
            )
            / n,
            "ece": sum(b["count"] * abs(b["predicted_mean"] - b["observed_mean"]) for b in bins)
            / n,
            "calibration_bins": bins,
        }
    edges = sorted({_quantile(sorted(prediction), i / 5) for i in range(6)})
    groups: dict[int, list[int]] = {}
    for i, value in enumerate(prediction):
        groups.setdefault(sum(value > edge for edge in edges[1:-1]), []).append(i)
    bins = [
        {
            "count": len(ids),
            "predicted_mean": sum(prediction[i] for i in ids) / len(ids),
            "observed_mean": sum(y[i] for i in ids) / len(ids),
        }
        for _group, ids in sorted(groups.items())
    ]
    residual = [p - t for p, t in zip(prediction, y, strict=True)]
    return {
        "mae": sum(abs(r) for r in residual) / n,
        **outcome_diagnostics(y, prediction, binary),
        "rmse": math.sqrt(sum(r * r for r in residual) / n),
        "mean_error": sum(residual) / n,
        "calibration_bins": bins,
        "mean_absolute_calibration_error": sum(
            b["count"] * abs(b["predicted_mean"] - b["observed_mean"]) for b in bins
        )
        / n,
    }


def _close(actual: Any, expected: Any, *, exact: bool = False) -> bool:
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _close(
                actual[key],
                expected[key],
                exact=key
                in {
                    "diagnostics_version",
                    "schema_version",
                    "decision_threshold",
                    "lower",
                    "upper",
                },
            )
            for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _close(a, e) for a, e in zip(actual, expected, strict=True)
        )
    if type(expected) is int or exact:
        return type(actual) is type(expected) and actual == expected
    if (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
    ):
        return math.isclose(actual, expected, rel_tol=1e-8, abs_tol=1e-10)
    return type(actual) is type(expected) and actual == expected


def review_release(model: ModelVersion) -> dict[str, Any]:
    try:
        task = next(
            task for task, model_type in TASK_TYPES.items() if model.model_type == model_type
        )
        registered = model.metrics or {}
        weights = _read(model.artifact_uri or "", registered["artifact_sha256"], parse=False)
        report = _read(registered["report_uri"], registered["report_sha256"])
        audit = _read(registered["release_evidence_uri"], registered["release_evidence_sha256"])
        if (
            type(audit["schema_version"]) is not int
            or audit["schema_version"] != 1
            or audit["source"] != "real"
            or audit["task"] != task
            or audit["weights_sha256"] != weights
            or report["weights_sha256"] != weights
            or report["task"] != task
            or report["split"] != "holdout"
            or report["eligible_population"] != POPULATIONS[task]
            or not _same(report["dataset_identity"], audit["dataset_identity"])
        ):
            raise ValueError("Wrong task, population, artifact or report binding")
        rows = [row for row in _validated_rows(audit) if row["task"] == task]
        groups = {split: [r for r in rows if r["split"] == split] for split in MIN_COUNTS}
        if any(not group for group in groups.values()):
            raise ValueError("Missing reviewed train/calibration/holdout examples")
        binary = task in {"completion", "pressure"}
        if binary and any(
            {r["target"]["value"] for r in group} != {0, 1} for group in groups.values()
        ):
            raise ValueError("Binary release requires both classes in every split")
        heldout = {r["lineage"]["canonical_play_id"]: r for r in groups["holdout"]}
        predictions = {}
        for prediction in audit["predictions"]:
            play = prediction["lineage"]["canonical_play_id"]
            row = heldout.get(play)
            if row is None or play in predictions or prediction["suppressed"] is not False:
                raise ValueError("Duplicate, missing or foreign heldout prediction")
            if any(
                not _same(prediction[key], row[key])
                for key in (
                    "task",
                    "feature_version",
                    "eligible_population",
                    "lineage",
                    "observation_clock",
                    "capture_regime",
                )
            ):
                raise ValueError("Prediction is not bound to the exact heldout feature snapshot")
            if prediction["weights_sha256"] != weights or prediction["feature_evidence_sha256"] != [
                row["features"][name]["evidence_sha256"] for name, _unit in FEATURES[task]
            ]:
                raise ValueError("Prediction artifact or input evidence hash differs")
            if "subject" in row and not _same(prediction.get("subject"), row["subject"]):
                raise ValueError("Prediction actor differs from the heldout feature snapshot")
            value = _number(prediction["value"])
            if binary and not 0 <= value <= 1:
                raise ValueError("Probability outside zero to one")
            predictions[play] = value
        if predictions.keys() != heldout.keys():
            raise ValueError("Complete immutable holdout predictions are required")
        ordered = groups["holdout"]
        truth = [_number(r["target"]["value"]) for r in ordered]
        predicted = [predictions[r["lineage"]["canonical_play_id"]] for r in ordered]
        train_mean = sum(_number(r["target"]["value"]) for r in groups["train"]) / len(
            groups["train"]
        )
        metrics = _metrics(truth, predicted, binary)
        baseline: dict[str, Any] = {
            "definition": "empirical_training_population_mean",
            "training_mean": train_mean,
            "metrics": _metrics(truth, [train_mean] * len(truth), binary),
        }
        if (
            report["count"] != len(truth)
            or not _close(report["metrics"], metrics)
            or not _close(report["baseline"], baseline)
        ):
            raise ValueError("Heldout report losses/calibration do not match immutable predictions")
        regimes = sorted({r.get("capture_regime", "unknown") for r in rows})
        missing_reasons = []
        if not regimes or not set(regimes) <= KNOWN_REGIMES:
            missing_reasons.append("qualified_capture_regimes_missing")

        def passes(values: dict[str, Any], reference: dict[str, Any]) -> bool:
            if binary:
                return bool(
                    values["brier"] < reference["brier"]
                    and values["log_loss"] < reference["log_loss"]
                    and values["ece"] <= MAX_ECE + 1e-12
                    and values["ece"] <= reference["ece"] + 1e-9
                )
            return bool(
                values["mae"] < reference["mae"]
                and values["rmse"] < reference["rmse"]
                and values["mean_absolute_calibration_error"]
                <= reference["mean_absolute_calibration_error"] + 1e-9
            )

        slices = {}
        for regime in regimes:
            grouped = {
                split: [r for r in group if r["capture_regime"] == regime]
                for split, group in groups.items()
            }
            enough = all(len(grouped[split]) >= count for split, count in MIN_COUNTS.items())
            if not enough:
                missing_reasons.append(f"independent_split_sample_minimums_missing_{regime}")
            if any(not grouped[split] for split in MIN_COUNTS):
                slices[regime] = {"passed": False, "reason": "split_missing"}
                continue
            slice_truth = [_number(r["target"]["value"]) for r in grouped["holdout"]]
            slice_pred = [
                predictions[r["lineage"]["canonical_play_id"]] for r in grouped["holdout"]
            ]
            mean = sum(_number(r["target"]["value"]) for r in grouped["train"]) / len(
                grouped["train"]
            )
            values, reference = (
                _metrics(slice_truth, slice_pred, binary),
                _metrics(slice_truth, [mean] * len(slice_truth), binary),
            )
            slices[regime] = {
                "metrics": values,
                "baseline_metrics": reference,
                "passed": enough and passes(values, reference),
            }
        population = _population(audit, groups)
        missing_reasons.extend(population["missing_data_reasons"])
        return {
            "kind": "expected_outcomes",
            "task": task,
            "model_id": str(model.id),
            "artifact_sha256": weights,
            "report_sha256": registered["report_sha256"],
            "release_evidence_sha256": registered["release_evidence_sha256"],
            "dataset_identity": audit["dataset_identity"],
            "source": "measured_human_holdout",
            "sample_count": len(truth),
            "metrics": metrics,
            "baseline": baseline,
            "capture_regimes": regimes,
            "feature_version": FEATURE_VERSION,
            "slices": slices,
            "population": population,
            "missing_data_reasons": missing_reasons,
            "passed": not missing_reasons
            and passes(metrics, baseline["metrics"])
            and all(s["passed"] for s in slices.values()),
            "thresholds": {
                "split_counts_per_film_category": MIN_COUNTS,
                "max_binary_ece": MAX_ECE,
                "binary_losses": "brier_and_log_loss_strictly_below_training_mean_baseline",
                "yardage_losses": (
                    "mae_and_rmse_strictly_below_baseline_with_calibration_error_no_worse"
                ),
            },
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
        raise HTTPException(
            422, detail=f"Expected-outcome release evidence is invalid: {exc}"
        ) from exc


def trusted_worker(actor: User, runtime: WorkerRuntime | None) -> bool:
    configured = get_settings().seed_worker_email.strip().casefold()
    return bool(
        configured
        and isinstance(actor.email, str)
        and actor.email.casefold() == configured
        and runtime is not None
        and runtime.registered_by == actor.id
    )


def _frozen_binding(model: ModelVersion, result: dict[str, Any]) -> dict[str, Any]:
    metadata = model.metrics or {}
    return {
        "model_id": str(model.id),
        "task": result["task"],
        "artifact_uri": model.artifact_uri,
        **{
            key: metadata[key]
            for key in (
                "artifact_sha256",
                "report_uri",
                "report_sha256",
                "release_evidence_uri",
                "release_evidence_sha256",
            )
        },
        "dataset_identity": result["dataset_identity"],
    }


async def bind_verification_completion(
    db: AsyncSession,
    job: ProcessingJob,
    actor: User,
    output: dict[str, Any],
    *,
    worker_id: str | None,
    lease_token: uuid.UUID | None,
    attempt_count: int | None,
) -> None:
    """Called before the generic job endpoint clears the successfully held lease."""
    runtime = await db.get(WorkerRuntime, worker_id) if worker_id else None
    if (
        not trusted_worker(actor, runtime)
        or job.status != JobStatus.running
        or not isinstance(job.lease_token, uuid.UUID)
        or job.lease_token != lease_token
        or job.leased_by != worker_id
        or attempt_count != job.attempt_count
        or not job.lease_expires_at
        or job.lease_expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(
            403, detail="Release verification requires the trusted current worker lease"
        )
    inputs = job.input_artifacts or {}
    challenge = await db.get(SystemSetting, f"learning:verification:{inputs.get('challenge_id')}")
    if (
        challenge is None
        or challenge.value.get("job_id") != str(job.id)
        or not _same(challenge.value.get("inputs"), inputs)
    ):
        raise HTTPException(422, detail="Release verification challenge is missing or changed")
    try:
        receipt = await to_thread.run_sync(
            _read, output["verification_receipt_uri"], output["verification_receipt_sha256"]
        )
        expected = {
            "schema_version": 1,
            "kind": "expected_outcome_verification_receipt",
            "verifier_version": VERIFIER_VERSION,
            "challenge_id": inputs["challenge_id"],
            "verification_job_id": str(job.id),
            "model_id": inputs["model_id"],
            "worker_id": worker_id,
            "attempt_count": attempt_count,
            "task": inputs["task"],
            "forward_outputs_matched": True,
            "source_media_verified": True,
            "source_clock_verified": True,
            "dataset_identity": inputs["dataset_identity"],
            **{
                key: inputs[key]
                for key in ("artifact_sha256", "report_sha256", "release_evidence_sha256")
            },
        }
        if any(not _same(receipt.get(key), value) for key, value in expected.items()):
            raise ValueError("Receipt does not match current challenge and worker attempt")
        _hash(receipt["recomputed_predictions_sha256"])
        if type(receipt["prediction_count"]) is not int or receipt["prediction_count"] < 1:
            raise ValueError("Empty independent verification")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise HTTPException(
            422, detail=f"Independent verification receipt is invalid: {exc}"
        ) from exc
    job.provenance = {
        **(job.provenance or {}),
        "expected_outcome_verification": {
            "trusted_execution": True,
            "challenge_id": inputs["challenge_id"],
            "worker_id": worker_id,
            "worker_principal_id": str(actor.id),
            "attempt_count": attempt_count,
            "receipt_sha256": output["verification_receipt_sha256"],
            "completed_at": datetime.now(UTC).isoformat(),
        },
    }


async def _completed_verification(
    db: AsyncSession, model: ModelVersion, challenge: SystemSetting, binding: dict[str, Any]
) -> dict[str, Any]:
    frozen = challenge.value
    job = await db.get(ProcessingJob, uuid.UUID(frozen["job_id"]))
    expected_inputs = {
        "training_variant": "verify_expected_outcomes",
        "challenge_id": frozen["challenge_id"],
        **binding,
    }
    if (
        job is None
        or job.status != JobStatus.succeeded
        or job.model_version_id != model.id
        or not _same(frozen["inputs"], expected_inputs)
        or not _same(job.input_artifacts, expected_inputs)
    ):
        raise HTTPException(
            422, detail="A completed current independent verification job is required"
        )
    proof = (job.provenance or {}).get("expected_outcome_verification") or {}
    output = job.output_artifacts or {}
    if (
        proof.get("trusted_execution") is not True
        or proof.get("challenge_id") != frozen["challenge_id"]
        or proof.get("attempt_count") != job.attempt_count
        or not proof.get("worker_principal_id")
        or proof.get("receipt_sha256") != output.get("verification_receipt_sha256")
    ):
        raise HTTPException(
            422, detail="Independent verification lacks a trusted current worker completion"
        )
    try:
        receipt = await to_thread.run_sync(
            _read, output["verification_receipt_uri"], output["verification_receipt_sha256"]
        )
        expected = {
            "schema_version": 1,
            "kind": "expected_outcome_verification_receipt",
            "verifier_version": VERIFIER_VERSION,
            "challenge_id": frozen["challenge_id"],
            "verification_job_id": str(job.id),
            "model_id": str(model.id),
            "worker_id": proof["worker_id"],
            "attempt_count": job.attempt_count,
            "task": binding["task"],
            "forward_outputs_matched": True,
            "source_media_verified": True,
            "source_clock_verified": True,
            "dataset_identity": binding["dataset_identity"],
            **{
                key: binding[key]
                for key in ("artifact_sha256", "report_sha256", "release_evidence_sha256")
            },
        }
        if any(not _same(receipt.get(key), value) for key, value in expected.items()):
            raise ValueError("Stale or mismatched verification receipt")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise HTTPException(
            422, detail=f"Independent verification receipt is unavailable: {exc}"
        ) from exc
    return {
        "verification_challenge_id": frozen["challenge_id"],
        "verification_job_id": str(job.id),
        "verification_receipt_sha256": output["verification_receipt_sha256"],
    }


async def evaluate_expected_outcomes(
    db: AsyncSession, model: ModelVersion, actor: User
) -> dict[str, Any]:
    """First request freezes+queues; a later request records the trusted completed result."""
    result = await to_thread.run_sync(review_release, model)
    binding = _frozen_binding(model, result)
    previous_id = (model.metrics or {}).get("verification_challenge_id")
    previous = (
        await db.get(SystemSetting, f"learning:verification:{previous_id}") if previous_id else None
    )
    if previous is not None and _same(previous.value.get("binding"), binding):
        job = await db.get(ProcessingJob, uuid.UUID(previous.value["job_id"]))
        if job is not None and job.status in {
            JobStatus.queued,
            JobStatus.running,
            JobStatus.paused,
        }:
            return {"status": job.status.value, "job_id": str(job.id), "challenge_id": previous_id}
        if job is not None and job.status == JobStatus.succeeded:
            receipt = await _completed_verification(db, model, previous, binding)
            evaluation_id = uuid.uuid4()
            result.update(
                **receipt,
                id=str(evaluation_id),
                created_at=datetime.now(UTC).isoformat(),
                created_by=str(actor.id),
            )
            db.add(
                SystemSetting(
                    key=f"learning:evaluation:{evaluation_id}", value=result, updated_by=actor.id
                )
            )
            model.metrics = {
                **(model.metrics or {}),
                "evaluation_id": str(evaluation_id),
                "benchmark": result,
            }
            await db.flush()
            return result
    if not get_settings().seed_worker_email.strip():
        raise HTTPException(
            422,
            detail="Configure the connected processor service account before release verification",
        )
    from app.workload import require_workload_capacity

    await require_workload_capacity("learning.train")(user=actor, db=db)
    challenge_id, job_id = uuid.uuid4(), uuid.uuid4()
    inputs = {
        "training_variant": "verify_expected_outcomes",
        "challenge_id": str(challenge_id),
        **binding,
    }
    job = ProcessingJob(
        id=job_id,
        job_type=JobType.train,
        status=JobStatus.queued,
        priority=0,
        pipeline_mode=PipelineMode.nightly,
        model_version_id=model.id,
        input_artifacts=inputs,
    )
    frozen = {
        "challenge_id": str(challenge_id),
        "job_id": str(job_id),
        "model_id": str(model.id),
        "binding": binding,
        "inputs": inputs,
        "created_by": str(actor.id),
        "created_at": datetime.now(UTC).isoformat(),
    }
    db.add(job)
    db.add(
        SystemSetting(
            key=f"learning:verification:{challenge_id}", value=frozen, updated_by=actor.id
        )
    )
    model.metrics = {**(model.metrics or {}), "verification_challenge_id": str(challenge_id)}
    await db.flush()
    return {"status": "queued", "job_id": str(job_id), "challenge_id": str(challenge_id)}


async def validate_expected_promotion(
    db: AsyncSession, model: ModelVersion, evaluation: dict[str, Any]
) -> dict[str, Any]:
    current = await to_thread.run_sync(review_release, model)
    challenge_id = evaluation.get("verification_challenge_id")
    if challenge_id != (model.metrics or {}).get("verification_challenge_id"):
        raise HTTPException(
            422, detail="Promotion requires the current independent verification challenge"
        )
    challenge = (
        await db.get(SystemSetting, f"learning:verification:{challenge_id}")
        if challenge_id
        else None
    )
    if challenge is None:
        raise HTTPException(422, detail="Independent verification challenge is unavailable")
    receipt = await _completed_verification(db, model, challenge, _frozen_binding(model, current))
    if any(not _same(receipt[key], evaluation.get(key)) for key in receipt):
        raise HTTPException(422, detail="Independent verification changed after evaluation")
    keys = (
        "kind",
        "task",
        "model_id",
        "artifact_sha256",
        "report_sha256",
        "release_evidence_sha256",
        "dataset_identity",
        "feature_version",
        "capture_regimes",
        "passed",
    )
    if current["passed"] is not True or any(
        not _same(current[key], evaluation.get(key)) for key in keys
    ):
        raise HTTPException(422, detail="Expected-outcome evidence changed after evaluation")
    name = TASK_TYPES[current["task"]]
    return {
        name: {
            "definition_version": DEFINITIONS[name]["version"],
            "feature_version": FEATURE_VERSION,
            "capture_regimes": current["capture_regimes"],
        }
    }
