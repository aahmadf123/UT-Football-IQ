"""Validate supplied real release evidence; never synthesize missing measurements.

Exit 0 only when every gate passes. Exit 1 means missing, invalid or failed
evidence. References are resolved relative to the manifest and SHA-256 checked.
This checks evidence integrity and release thresholds, not annotation truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

CATEGORIES = ("practice", "coach_game", "television")
TASKS = ("segmentation", "events", "tracking", "identity", "spatial", "clear_lane",
         "interactions", "aggregation", "gpu_timing", "predictive", "production_e2e", "workspace")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def numbers(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    try:
        valid = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and v >= 0 for v in values)
    except OverflowError:
        valid = False
    if not valid:
        return []
    return [float(v) for v in values]


def numeric(value: Any) -> bool:
    """JSON booleans are never measurements, even though bool subclasses int."""
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def protect_references(value: Any, root: Path, protected: set[Path]) -> None:
    """Collect paths before validation, including nested references in reports."""
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and "sha256" in value:
            protected.add((root / Path(value["path"])).resolve())
        for child in value.values():
            protect_references(child, root, protected)
    elif isinstance(value, list):
        for child in value:
            protect_references(child, root, protected)


def output_conflicts(output: Path, protected: set[Path]) -> bool:
    return output.resolve() in protected or (output.exists() and any(
        path.exists() and output.samefile(path) for path in protected
    ))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    offset = (len(ordered) - 1) * fraction
    low, high = math.floor(offset), math.ceil(offset)
    return ordered[low] + (ordered[high] - ordered[low]) * (offset - low)


def wilson_lower(correct: int, total: int) -> float:
    """Lower endpoint of the two-sided 95% Wilson score interval."""
    if total <= 0 or not 0 <= correct <= total:
        return 0.0
    z = 1.959963984540054
    rate = correct / total
    return (rate + z * z / (2 * total) - z * math.sqrt(
        rate * (1 - rate) / total + z * z / (4 * total * total)
    )) / (1 + z * z / total)


def valid_dense_track(track: Any) -> bool:
    if not isinstance(track, dict) or not track.get("target_id"):
        return False
    points = track.get("points")
    if not isinstance(points, list) or not points:
        return False
    previous = -1.0
    for point in points:
        if not isinstance(point, dict):
            return False
        timestamp = numbers([point.get("timestamp_seconds")])
        box = numbers(point.get("bbox"))
        if not timestamp or timestamp[0] <= previous or len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
            return False
        previous = timestamp[0]
    return True


def dense_coverage_valid(play: dict[str, Any], source_id: str, media_sha256: str,
                         evidence: "Evidence") -> bool:
    """Density requires complete assigned native frames and explicit human coverage."""
    if not play.get("dense_coverage"):
        return False
    coverage = evidence.read(play["dense_coverage"], f"dense coverage {source_id}/{play['id']}")
    if not coverage:
        return False
    reviewers = coverage.get("reviewer_ids")
    if (coverage.get("evidence_kind") != "human_reviewed_dense_coverage"
            or coverage.get("source_id") != source_id or coverage.get("media_sha256") != media_sha256
            or coverage.get("source_play_id") != play["id"]
            or coverage.get("canonical_play_id") != play["canonical_play_id"]
            or any(coverage.get(key) is not True for key in (
                "completed", "all_visible_players_annotated", "occlusion_offscreen_records_reviewed"))
            or not isinstance(reviewers, list) or not reviewers
            or any(not isinstance(reviewer, str) or not reviewer.strip() for reviewer in reviewers)):
        return False
    index = evidence.read(coverage.get("native_frame_index"), f"native frames {source_id}/{play['id']}")
    if (not index or index.get("evidence_kind") != "source_frame_index"
            or index.get("source_id") != source_id or index.get("media_sha256") != media_sha256
            or not numeric(index.get("start_seconds")) or not numeric(index.get("end_seconds"))
            or not 0 <= index["start_seconds"] <= play["start_seconds"]
            or index["end_seconds"] < play["end_seconds"]
            or not count(index.get("width")) or index["width"] <= 0
            or not count(index.get("height")) or index["height"] <= 0):
        return False
    pts = numbers(index.get("frame_pts_seconds"))
    if (not pts or any(right <= left for left, right in zip(pts, pts[1:]))
            or any(stamp < index["start_seconds"] or stamp > index["end_seconds"] for stamp in pts)):
        return False
    # Six-decimal source timestamps allow encoder/browser decimal serialization,
    # never neighboring-frame interpolation or nominal-FPS reconstruction.
    expected = {round(stamp, 6) for stamp in pts if play["start_seconds"] <= stamp <= play["end_seconds"]}
    frames = coverage.get("frames")
    if not expected or not isinstance(frames, list) or len(frames) != len(expected):
        return False
    by_time: dict[float, set[str]] = {}
    for frame in frames:
        if not isinstance(frame, dict) or not numeric(frame.get("timestamp_seconds")):
            return False
        stamp = round(frame["timestamp_seconds"], 6)
        targets = frame.get("visible_target_ids")
        if (stamp not in expected or stamp in by_time or not isinstance(targets, list)
                or any(not isinstance(target, str) or not target.strip() for target in targets)
                or len(set(targets)) != len(targets)):
            return False
        by_time[stamp] = set(targets)
    tracks = play.get("dense_tracks")
    if not isinstance(tracks, list) or not tracks or not all(valid_dense_track(track) for track in tracks):
        return False
    target_ids = [track["target_id"] for track in tracks]
    if any(not isinstance(target, str) for target in target_ids) or len(set(target_ids)) != len(target_ids):
        return False
    observed: dict[float, set[str]] = defaultdict(set)
    for track in tracks:
        signals: dict[float, str] = {}
        for point in track["points"]:
            stamp = round(point["timestamp_seconds"], 6)
            if (stamp not in expected or not isinstance(point.get("shot_id"), str) or not point["shot_id"].strip()
                    or point.get("visibility_state", "visible") != "visible"
                    or point["bbox"][2] > index["width"] or point["bbox"][3] > index["height"]):
                return False
            observed[stamp].add(track["target_id"])
            signals[stamp] = "visible"
        for marker in track.get("visibility_events", []):
            if (not isinstance(marker, dict) or not numeric(marker.get("timestamp_seconds"))
                    or not play["start_seconds"] <= marker["timestamp_seconds"] <= play["end_seconds"]
                    or marker.get("visibility_state") not in {"occluded", "offscreen"}
                    or not isinstance(marker.get("shot_id"), str) or not marker["shot_id"].strip()):
                return False
            stamp = round(marker["timestamp_seconds"], 6)
            if stamp not in expected or stamp in signals:
                return False
            signals[stamp] = marker["visibility_state"]
        for stamp, visible in by_time.items():
            if track["target_id"] in visible:
                continue
            preceding = [time for time in signals if time <= stamp]
            if not preceding or signals[max(preceding)] not in {"occluded", "offscreen"}:
                return False
    return all(observed[stamp] == visible for stamp, visible in by_time.items())


class Evidence:
    def __init__(self, root: Path, protected: set[Path] | None = None) -> None:
        self.root = root
        self.protected = protected if protected is not None else set()
        self.errors: list[str] = []
        self.verified: dict[str, str] = {}

    def read(self, reference: Any, label: str, *, as_json: bool = True) -> Any:
        if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
            self.errors.append(f"{label}: missing evidence reference")
            return None
        protect_references(reference, self.root, self.protected)
        expected = reference.get("sha256", "")
        if not isinstance(expected, str) or not SHA256.fullmatch(expected):
            self.errors.append(f"{label}: missing or invalid SHA-256")
            return None
        path = Path(reference["path"])
        if not path.is_absolute():
            path = self.root / path
        try:
            path = path.resolve(strict=True)
            key = str(path)
            digest = self.verified.get(key)
            if digest is None:
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(block)
                digest = hasher.hexdigest()
                self.verified[key] = digest
            if digest != expected:
                self.errors.append(f"{label}: SHA-256 mismatch")
                return None
            if not as_json:
                return True
            packet = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_nonfinite_json)
            if not isinstance(packet, dict):
                raise ValueError("JSON evidence must be an object")
            protect_references(packet, self.root, self.protected)
            return packet
        except (OSError, ValueError) as exc:
            self.errors.append(f"{label}: {type(exc).__name__}")
            return None


def evaluate(manifest: dict[str, Any], root: Path, protected: set[Path] | None = None) -> dict[str, Any]:
    evidence = Evidence(root, protected)
    gates: list[dict[str, Any]] = []

    def gate(name: str, passed: bool, measured: Any, required: Any) -> None:
        gates.append({"name": name, "passed": bool(passed), "measured": measured,
                      "required": required})

    if manifest.get("schema_version") != 1:
        evidence.errors.append("manifest: schema_version must be 1")
    splits = manifest.get("splits", {})
    split_sets = {name: set(splits.get(name, [])) for name in ("train", "calibration", "holdout")}
    overlap = set()
    for left, right in (("train", "calibration"), ("train", "holdout"), ("calibration", "holdout")):
        overlap.update(split_sets[left] & split_sets[right])
    gate("disjoint_source_groups", not overlap and bool(split_sets["holdout"]),
         {"overlap": sorted(overlap), "holdout_groups": len(split_sets["holdout"])},
         "Hold out entire independent sessions/games and all alternate views")
    plays: dict[str, dict[str, Any]] = {}
    labels_by_case: dict[tuple[str, str, str], dict[str, Any]] = {}
    label_hashes: dict[str, set[str]] = defaultdict(set)
    category_groups: dict[str, set[str]] = defaultdict(set)
    canonical_splits: dict[str, set[str]] = defaultdict(set)
    media_splits: dict[str, set[str]] = defaultdict(set)
    media_groups: dict[str, set[str]] = defaultdict(set)
    seen_sources: set[str] = set()
    source_groups: dict[str, str] = {}
    dense_cases: list[tuple[dict[str, Any], str, str, str]] = []
    for source in manifest.get("sources", []):
        sid, group, category = source.get("id"), source.get("group_id"), source.get("category")
        if not sid or sid in seen_sources or not group or category not in CATEGORIES:
            evidence.errors.append("source: unique id, group_id and supported category required")
            continue
        seen_sources.add(sid)
        source_groups[sid] = group
        assigned = {name for name, groups in split_sets.items() if group in groups}
        if len(assigned) != 1:
            evidence.errors.append(f"source {sid}: requires exactly one source-group split")
        media = source.get("media", {})
        media_valid = evidence.read(media, f"source {sid} media", as_json=False)
        annotation = evidence.read(source.get("labels"), f"source {sid} labels")
        if not media_valid or not annotation:
            continue
        media_splits[media["sha256"]].update(assigned)
        media_groups[media["sha256"]].add(group)
        if (annotation.get("evidence_kind") != "human_reviewed" or annotation.get("reviewed") is not True
                or not annotation.get("reviewer_ids") or annotation.get("source_id") != sid
                or annotation.get("media_sha256") != media["sha256"]):
            evidence.errors.append(f"source {sid}: reviewed human labels must bind to this media hash")
            continue
        label_hashes[group].add(source["labels"]["sha256"])
        if group in split_sets["holdout"]:
            category_groups[category].add(group)
        for play in annotation.get("plays", []):
            canonical = play.get("canonical_play_id")
            boundary = [play.get(key) for key in ("start_seconds", "snap_seconds", "end_seconds")]
            if (not canonical or not play.get("id") or len(numbers(boundary)) != 3
                    or not boundary[0] <= boundary[1] < boundary[2]):
                evidence.errors.append(f"source {sid}: play requires canonical id and valid annotated boundaries")
                continue
            canonical_splits[canonical].update(assigned)
            if group not in split_sets["holdout"]:
                continue
            previous = plays.get(canonical)
            if previous and previous["category"] != category:
                evidence.errors.append(f"canonical play {canonical}: inconsistent film category")
            else:
                plays[canonical] = {**play, "category": category,
                                    "reviewer_ids": play.get("reviewer_ids", annotation["reviewer_ids"])}
            dense_cases.append((play, sid, media["sha256"], category))
            for identity in play.get("identities", []):
                if identity.get("target_id") and identity.get("player_id") and identity.get("canonical_entity_id"):
                    labels_by_case[(sid, str(play["id"]), str(identity["target_id"]))] = {
                        **identity, "canonical_play_id": canonical,
                    }
    leaked = [key for table in (media_splits, canonical_splits)
              for key, values in table.items() if len(values) > 1]
    if any(len(groups) > 1 for groups in media_groups.values()):
        evidence.errors.append("Identical source media cannot count as different independent source groups")
    gate("no_media_or_play_leakage", not leaked, {"leaked_references": len(leaked)}, 0)
    counts = Counter(play["category"] for play in plays.values())
    independent_groups = set().union(*category_groups.values()) if category_groups else set()
    gate("heldout_population", len(plays) >= 240 and len(independent_groups) >= 12
         and all(counts[c] >= 80 for c in CATEGORIES)
         and all(len(category_groups[c]) >= 4 for c in CATEGORIES),
         {"canonical_plays": len(plays), "independent_groups": len(independent_groups), "plays_by_category": dict(counts),
          "independent_groups_by_category": {c: len(category_groups[c]) for c in CATEGORIES}},
         {"canonical_plays": 240, "independent_groups": 12, "plays_per_category": 80, "independent_groups_per_category": 4})
    reviewed = sum(play.get("double_labeled") is True and play.get("adjudicated") is True
                   and len(set(play.get("reviewer_ids", []))) >= 2
                   for play in plays.values())
    gate("double_label_adjudication", bool(plays) and reviewed >= math.ceil(len(plays) * .2),
         reviewed, {"fraction": .2, "adjudicated": True})
    dense_ids: dict[str, str] = {}
    for play, sid, digest, category in dense_cases:
        if dense_coverage_valid(play, sid, digest, evidence):
            dense_ids[play["canonical_play_id"]] = category
    dense = Counter(dense_ids.values())
    gate("dense_tracking_population", all(dense[c] >= 16 for c in CATEGORIES), dict(dense),
         {"plays": 48, "plays_per_category": 16,
          "density": "hashed actual native-frame index, complete human coverage and visibility records"})

    reports: dict[str, dict[str, Any]] = {}
    predictions: dict[str, dict[str, Any]] = {}
    for item in manifest.get("evaluations", []):
        task = item.get("task")
        if task not in TASKS or task in reports:
            evidence.errors.append("evaluation: unsupported or duplicate task")
            continue
        report = evidence.read(item.get("report"), f"{task} report")
        predicted = evidence.read(item.get("predictions"), f"{task} predictions")
        if not report or not predicted:
            continue
        groups = set(report.get("source_groups", []))
        expected_labels = set().union(*(label_hashes[g] for g in groups)) if groups else set()
        weights = report.get("model_weights", [])
        weights_valid = bool(weights) and all(
            evidence.read(ref, f"{task} model weights", as_json=False) for ref in weights
        )
        samples = predicted.get("samples", [])
        samples_valid = isinstance(samples, list) and bool(samples) and all(
            isinstance(sample, dict) and source_groups.get(sample.get("source_id")) in groups
            for sample in samples
        )
        if (report.get("evidence_source") != "real" or predicted.get("evidence_source") != "real"
                or not isinstance(report.get("evaluator_version"), str) or not report["evaluator_version"].strip()
                or not groups or not samples_valid
                or not groups.issubset(split_sets["holdout"])
                or not expected_labels or set(report.get("label_sha256", [])) != expected_labels
                or report.get("predictions_sha256") != item["predictions"].get("sha256")
                or (task not in {"aggregation", "production_e2e", "workspace"} and not weights_valid)):
            evidence.errors.append(f"{task}: real evaluation lineage, heldout labels/predictions and actual model bytes required")
            continue
        reports[task], predictions[task] = report, predicted
    for task in TASKS:
        gate(f"{task}_evidence", task in reports, "verified" if task in reports else "missing", "hashed real evaluation")

    for category in CATEGORIES:
        segmentation = reports.get("segmentation", {}).get("categories", {}).get(category, {})
        tp, fp, fn = (segmentation.get(key, 0) for key in ("true_positive", "false_positive", "false_negative"))
        valid_counts = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (tp, fp, fn))
        precision = tp / (tp + fp) if valid_counts and tp + fp else None
        recall = tp / (tp + fn) if valid_counts and tp + fn else None
        total, complete = segmentation.get("total_live_actions", 0), segmentation.get("complete_live_actions", 0)
        completeness = complete / total if isinstance(total, int) and isinstance(complete, int) and not isinstance(total, bool) and not isinstance(complete, bool) and total > 0 and 0 <= complete <= total else None
        gate(f"segmentation_{category}", precision is not None and recall is not None and completeness is not None
             and precision >= .95 and recall >= .95 and completeness >= .99,
             {"precision": precision, "recall": recall, "complete_live_action": completeness},
             {"precision": .95, "recall": .95, "complete_live_action": .99})
        events = reports.get("events", {}).get("categories", {}).get(category, {})
        for event in ("snap", "release"):
            errors = numbers(events.get(f"{event}_absolute_errors_seconds"))
            gate(f"{event}_timing_{category}", bool(errors) and median(errors) <= .1 and percentile(errors, .95) <= .25,
                 {"n": len(errors), "median_seconds": median(errors) if errors else None,
                  "p95_seconds": percentile(errors, .95) if errors else None}, {"median_seconds": .1, "p95_seconds": .25})
        tracking = reports.get("tracking", {}).get("categories", {}).get(category, {})
        gate(f"tracking_{category}", all(numeric(tracking.get(key)) for key in ("idf1", "hota", "visibility_coverage", "visible_player_recall", "localization_error"))
             and 90 <= tracking["idf1"] <= 100 and 70 <= tracking["hota"] <= 100
             and 0 <= tracking["visibility_coverage"] <= 1 and 0 <= tracking["visible_player_recall"] <= 1
             and tracking["localization_error"] >= 0 and count(tracking.get("identity_switches")) and count(tracking.get("fragmentation")),
             tracking or None, {"idf1": 90, "hota": 70, "coverage_and_error_reporting": True})

    accepted = correct = 0
    case_outcomes: dict[tuple[str, str], list[bool]] = defaultdict(list)
    eligible_cases: set[tuple[str, str]] = set()
    covered_cases: set[tuple[str, str]] = set()
    for truth in labels_by_case.values():
        if truth.get("readable") is True and truth.get("team_resolved") is True:
            eligible_cases.add((truth["canonical_play_id"], truth["canonical_entity_id"]))
    for prediction in predictions.get("identity", {}).get("samples", []):
        key = tuple(str(prediction.get(k, "")) for k in ("source_id", "play_id", "target_id"))
        truth = labels_by_case.get(key)
        canonical_key = (truth["canonical_play_id"], truth["canonical_entity_id"]) if truth else None
        if prediction.get("accepted") is not True:
            continue
        if not truth:
            evidence.errors.append("identity: accepted prediction has no reviewed identity label")
            continue
        case_outcomes[canonical_key].append(prediction.get("player_id") == truth["player_id"])
        if canonical_key in eligible_cases:
            covered_cases.add(canonical_key)
    accepted = len(case_outcomes)
    # Conflicting accepted views cannot be hidden by selecting the first one.
    correct = sum(all(outcomes) for outcomes in case_outcomes.values())
    eligible_ids = len(eligible_cases)
    covered = len(covered_cases)
    precision = correct / accepted if accepted else 0
    lower = wilson_lower(correct, accepted)
    coverage = covered / eligible_ids if eligible_ids else 0
    gate("automatic_identity", accepted >= 2000 and precision >= .995 and lower >= .99 and coverage >= .9,
         {"accepted": accepted, "correct": correct, "precision": precision, "wilson_95_lower": lower,
          "readable_team_resolved_cases": eligible_ids, "coverage": coverage},
         {"accepted_target": 2000, "precision": .995, "wilson_95_lower": .99, "coverage": .9})
    for task, field, thresholds in (("spatial", "location_absolute_errors_yards", (.35, .75, .95)),
                                     ("clear_lane", "width_absolute_errors_yards", (None, .30, .9))):
        errors = numbers(reports.get(task, {}).get(field))
        mid, tail, quantile = thresholds
        gate(f"{task}_accuracy", bool(errors) and (mid is None or median(errors) <= mid)
             and percentile(errors, quantile) <= tail,
             {"n": len(errors), "median_yards": median(errors) if errors else None,
              f"p{int(quantile*100)}_yards": percentile(errors, quantile) if errors else None},
             {"median_yards": mid, f"p{int(quantile*100)}_yards": tail})
    interactions = reports.get("interactions", {}).get("specialists", [])
    gate("interaction_precision", bool(interactions) and all(
        isinstance(row.get("name"), str) and bool(row["name"].strip())
        and all(numeric(row.get(key)) for key in ("accepted_precision", "recall", "coverage"))
        and .95 <= row["accepted_precision"] <= 1
        and 0 <= row["recall"] <= 1 and 0 <= row["coverage"] <= 1
        and bool(numbers(row.get("onset_absolute_errors_seconds")))
        for row in interactions), interactions or None, {"each_specialist_precision": .95, "recall_coverage_onset_reporting": True})
    aggregation = reports.get("aggregation", {}).get("scenarios", {})
    regressions = ("additional_views", "fragmented_tracks", "reprocessing", "correction_preservation",
                   "long_tracking_gaps", "mirrored_views", "duplicate_jerseys", "same_number_opponents",
                   "replays", "missing_cards", "fractional_vfr_timing", "late_game_seeks", "rollouts",
                   "early_pressure", "occlusion", "queue_saturation", "gpu_interruption")
    gate("aggregation_invariance", all(aggregation.get(name) is True for name in regressions),
         aggregation or None, "all canonical-count and correction regressions pass")
    timing = reports.get("gpu_timing", {})
    fast = [r for r in timing.get("fast_runs", []) if numeric(r.get("source_minutes")) and 0 < r["source_minutes"] <= 5 and r.get("height") == 1080]
    durations = numbers([r.get("elapsed_minutes") for r in fast])
    detailed = timing.get("detailed_runs", [])
    gate("gpu_fast_p95", isinstance(timing.get("gpu_name"), str) and bool(timing["gpu_name"].strip()) and bool(durations)
         and all(value > 0 for value in durations)
         and any(r.get("source_minutes") == 5 for r in fast) and percentile(durations, .95) <= 10,
         {"gpu": timing.get("gpu_name"), "n": len(durations), "p95_minutes": percentile(durations, .95) if durations else None},
         {"up_to_5_source_minutes_at_1080p": True, "p95_elapsed_minutes": 10})
    gate("gpu_overnight", bool(detailed) and any(numeric(r.get("source_minutes")) and numeric(r.get("elapsed_minutes"))
         and r["source_minutes"] >= 90 and 0 < r["elapsed_minutes"] <= 480 for r in detailed),
         detailed or None, {"source_minutes_across_cameras": 90, "elapsed_minutes_max": 480})
    models = reports.get("predictive", {}).get("models", [])
    required_models = {"completion", "expected_rushing", "expected_yac", "contextual_pressure"}
    passing_models = {r.get("name") for r in models if count(r.get("sample_count")) and r["sample_count"] > 0
                      and all(isinstance(r.get(k), (int, float)) and not isinstance(r[k], bool) and math.isfinite(r[k]) for k in ("loss", "baseline_loss", "calibration_error", "baseline_calibration_error"))
                      and 0 <= r["loss"] < r["baseline_loss"] and 0 <= r["calibration_error"] <= r["baseline_calibration_error"]}
    gate("predictive_baselines_and_calibration", required_models.issubset(passing_models), sorted(passing_models), sorted(required_models))
    trace = reports.get("production_e2e", {})
    components = ("frontend", "edge", "backend", "worker", "database_schema", "models", "artifacts")
    checks = ("upload", "durable_queue", "gpu_claim", "worker_results", "signed_range_playback",
              "normal_speed_playback", "evidence_drilldown", "analytics_query", "report_download", "correction_preservation")
    gate("production_upload_to_result", all(isinstance(trace.get("component_versions", {}).get(k), str)
         and bool(trace["component_versions"][k].strip()) for k in components)
         and all(trace.get("checks", {}).get(k) is True for k in checks),
         {"component_versions": trace.get("component_versions"), "checks": trace.get("checks")},
         {"components": list(components), "checks": list(checks)})
    workspace = reports.get("workspace", {}).get("checks", {})
    workspace_checks = ("six_primary_destinations", "persistent_film_workspace", "coach_analyst_layouts",
                        "url_saved_personal_precedence", "next_play_context", "angle_alignment",
                        "signed_url_renewal_state", "keyboard_frame_playback", "tablet_accessibility",
                        "shared_filters", "cohort_denominators", "position_views", "chart_film_drilldown",
                        "accessible_chart_tables", "export_downloads", "unknown_coverage_visible")
    gate("workspace_task_acceptance", all(workspace.get(key) is True for key in workspace_checks),
         workspace or None, list(workspace_checks))
    gate("evidence_integrity", not evidence.errors, evidence.errors, "all references, hashes and lineage valid")
    return {"schema_version": 1, "status": "passed" if all(g["passed"] for g in gates) else "not_ready",
            "passed": all(g["passed"] for g in gates), "gates": gates,
            "verified_artifacts": len(evidence.verified),
            "limitations": "Threshold and integrity checks do not independently establish annotation truth or actual model accuracy."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    protected = {args.manifest.resolve()}

    def refuse_overwrite() -> int:
        print(json.dumps({"passed": False, "status": "not_ready",
                          "errors": ["Output cannot overwrite the manifest or supplied evidence"]}))
        return 1

    try:
        if args.output and output_conflicts(args.output, protected):
            return refuse_overwrite()
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"), parse_constant=reject_nonfinite_json)
        protect_references(manifest, args.manifest.resolve().parent, protected)
        if args.output and output_conflicts(args.output, protected):
            return refuse_overwrite()
        result = evaluate(manifest, args.manifest.resolve().parent, protected)
        if args.output and output_conflicts(args.output, protected):
            return refuse_overwrite()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
        result = {"schema_version": 1, "status": "not_ready", "passed": False,
                  "errors": [f"Invalid evidence manifest: {type(exc).__name__}: {exc}"]}
        # Failed input/protection discovery cannot authorize writing any output.
        print(json.dumps(result, indent=2, allow_nan=False))
        return 1
    output = json.dumps(result, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
