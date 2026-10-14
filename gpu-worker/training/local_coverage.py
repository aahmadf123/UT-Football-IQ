"""Fit an experimental coverage head locally from reviewed same-clip examples.

python -m training.local_coverage --labels labels.jsonl --features features.jsonl --output out

Features must explicitly assign session-disjoint train, calibration and holdout
splits. Labels are human truth; predictions and other recordings never become
truth. The holdout is scored only after fitting and calibration. No model is
promoted by this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.calibration.calibrated_output import multiclass_ece
from pipeline.coverage.gnn_classifier import COVERAGE_CLASSES, CoverageGNN
from pipeline.coverage.train_coverage_gnn import (
    _probs_matrix,
    graph_from_dict,
    graphs_to_embeddings,
)
from pipeline.runtime import check_control
from pipeline.spatial import feature_schema as fs

MIN_SPLIT_COUNTS = {"train": 30, "calibration": 10, "holdout": 20}
LABEL_TYPES = {"coverage_shell", "coverage_tag"}


class TrainingDataError(ValueError):
    """Training cannot proceed without valid independently labelled examples."""

    def __init__(self, reason: str, *, status: str = "invalid_data") -> None:
        self.status = status
        super().__init__(reason)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            check_control()
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise TrainingDataError("Dataset rows must be JSON objects")
                records.append(item)
    return records


def _class_label(record: dict[str, Any]) -> str:
    value = record.get("label_value", {})
    if not isinstance(value, dict):
        raise TrainingDataError("Human coverage value must be an object")
    labels = {str(value[key]) for key in ("shell", "coverage") if value.get(key) is not None}
    if len(labels) != 1 or not labels.issubset(COVERAGE_CLASSES):
        raise TrainingDataError("Coverage truth must name one supported coverage class")
    return labels.pop()


def _validated_examples(
    labels: list[dict[str, Any]], features: list[dict[str, Any]], *, allow_synthetic_test: bool
) -> tuple[list[dict[str, Any]], str]:
    packets: dict[str, dict[str, Any]] = {}
    for packet in features:
        cid = str(packet.get("clip_id", ""))
        if not cid or cid in packets:
            raise TrainingDataError("Feature packets require unique clip ids")
        packets[cid] = packet
    examples = []
    seen_truth: dict[str, str] = {}
    source = "real"
    for label in labels:
        check_control()
        if label.get("label_type") not in LABEL_TYPES:
            continue
        if label.get("source") not in {"human", "manual"} or not label.get("annotated_by"):
            raise TrainingDataError("Only attributed human coverage labels can train a candidate")
        cid = str(label.get("clip_id", ""))
        truth = _class_label(label)
        if cid in seen_truth:
            if seen_truth[cid] != truth:
                raise TrainingDataError("Conflicting truth revisions: choose one reviewed snapshot")
            continue
        seen_truth[cid] = truth
        packet = packets.get(cid)
        if packet is None:
            raise TrainingDataError(f"Same-clip feature graph is missing for {cid}", status="insufficient_data")
        packet_source = packet.get("source")
        if packet_source != "real":
            if not (allow_synthetic_test and packet_source == "synthetic_test"):
                raise TrainingDataError("Synthetic or unverified feature packets cannot train real models")
            source = "synthetic_test"
        if packet.get("analytics_safe") is not True:
            raise TrainingDataError("Coverage features require confirmed field mapping")
        if not packet.get("session_id") or packet.get("split") not in MIN_SPLIT_COUNTS:
            raise TrainingDataError("Each graph requires its session and explicit train/calibration/holdout split")
        graph_dict = packet.get("graph")
        if not isinstance(graph_dict, dict) or graph_dict.get("schema_version") != fs.SCHEMA_VERSION:
            raise TrainingDataError("Feature graph schema is incompatible")
        try:
            graph = graph_from_dict(graph_dict)
        except (ValueError, TypeError, KeyError) as exc:
            raise TrainingDataError("Feature graph is malformed") from exc
        if graph.node_features.shape[0] < 5 or not np.isfinite(graph.node_features).all():
            raise TrainingDataError("Feature graph needs at least five observed defenders and finite features")
        if not np.isfinite(graph.edge_features).all() or graph.edge_index.shape[1] != len(graph.edge_features):
            raise TrainingDataError("Feature graph edges are malformed")
        if graph.edge_index.size and (graph.edge_index.min() < 0 or graph.edge_index.max() >= len(graph.node_features)):
            raise TrainingDataError("Feature graph edge refers to an absent player")
        examples.append({"clip_id": cid, "truth_label_id": str(label.get("label_id", label.get("id", ""))),
                         "session_id": str(packet["session_id"]), "split": packet["split"],
                         "truth": truth, "graph": graph, "graph_dict": graph_dict})
    counts = Counter(row["split"] for row in examples)
    if any(counts[split] < minimum for split, minimum in MIN_SPLIT_COUNTS.items()):
        raise TrainingDataError(
            f"Insufficient independent labels: {dict(counts)}; required {MIN_SPLIT_COUNTS}",
            status="insufficient_data",
        )
    sessions: dict[str, str] = {}
    for row in examples:
        previous = sessions.setdefault(row["session_id"], row["split"])
        if previous != row["split"]:
            raise TrainingDataError("Session leakage: recordings from one session cross dataset splits")
    train_counts = Counter(row["truth"] for row in examples if row["split"] == "train")
    if len(train_counts) < 2 or min(train_counts.values()) < 5:
        raise TrainingDataError("Train at least two coverage classes with five examples each", status="insufficient_data")
    for split in ("calibration", "holdout"):
        split_counts = Counter(row["truth"] for row in examples if row["split"] == split)
        if len(split_counts) < 2 or min(split_counts.values()) < 2 or not split_counts.keys() <= train_counts.keys():
            raise TrainingDataError(f"{split} needs two supported classes with at least two examples each",
                                    status="insufficient_data")
    return sorted(examples, key=lambda row: row["clip_id"]), source


def _macro_f1(truth: np.ndarray, predicted: np.ndarray) -> float:
    scores = []
    for category in np.unique(truth):
        correct = np.count_nonzero((truth == category) & (predicted == category))
        support = np.count_nonzero(truth == category) + np.count_nonzero(predicted == category)
        scores.append(2 * correct / support if support else 0.)
    return float(np.mean(scores))


def _atomic_json(path: Path, body: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".candidate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(body, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def train_candidate(
    label_records: list[dict[str, Any]], feature_records: list[dict[str, Any]],
    output_dir: Path | str, *, seed: int = 0, allow_synthetic_test: bool = False,
) -> dict[str, Any]:
    """Fit, calibrate, evaluate and persist an experimental CPU candidate."""
    examples, source = _validated_examples(label_records, feature_records,
                                            allow_synthetic_test=allow_synthetic_test)
    serialized = [{k: v for k, v in row.items() if k != "graph"} for row in examples]
    digest = hashlib.sha256(json.dumps(serialized, sort_keys=True).encode()).hexdigest()
    groups = {split: [row for row in examples if row["split"] == split]
              for split in MIN_SPLIT_COUNTS}
    index = {label: i for i, label in enumerate(COVERAGE_CLASSES)}
    emb = {split: graphs_to_embeddings([row["graph"] for row in rows]) for split, rows in groups.items()}
    truth = {split: np.array([index[row["truth"]] for row in rows], dtype=np.int64)
             for split, rows in groups.items()}
    check_control()
    model = CoverageGNN(provenance={"usage": "local-human-reviewed-candidate",
                                   "source": source, "validated": False, "dataset_sha256": digest})
    model.fit(emb["train"], truth["train"], seed=seed)
    check_control()
    model.calibrate(emb["calibration"], truth["calibration"])
    check_control()
    probabilities = _probs_matrix(model, emb["holdout"])
    predicted = np.argmax(probabilities, axis=1)
    accuracy = float(np.mean(predicted == truth["holdout"]))
    macro_f1 = _macro_f1(truth["holdout"], predicted)
    ece = multiclass_ece(probabilities, truth["holdout"])
    majority = Counter(truth["train"].tolist()).most_common(1)[0][0]
    baseline = float(np.mean(truth["holdout"] == majority))
    passed = source == "real" and accuracy >= .9 and macro_f1 >= .85 and ece <= .1 and accuracy >= baseline + .02
    provenance = {**model.provenance, "train_clip_ids": [row["clip_id"] for row in groups["train"]],
                  "calibration_clip_ids": [row["clip_id"] for row in groups["calibration"]],
                  "holdout_clip_ids": [row["clip_id"] for row in groups["holdout"]]}
    model.provenance = provenance
    report = {"status": "candidate", "model_type": "coverage", "actual_model": "coverage-gnn-numpy",
              "source": source, "device": "cpu", "dataset_sha256": digest,
              "split_counts": {key: len(rows) for key, rows in groups.items()},
              "session_counts": {key: len({row["session_id"] for row in rows}) for key, rows in groups.items()},
              "accuracy": accuracy, "macro_f1": macro_f1, "ece": float(ece),
              "majority_baseline_accuracy": baseline, "release_gate_passed": passed,
              "promoted": False, "supported_classes": sorted({row["truth"] for row in groups["train"]}),
              "note": "Experimental candidate; independent backend benchmark and admin promotion required."}
    predictions = [{"clip_id": row["clip_id"], "truth_label_id": row["truth_label_id"],
                    "label_type": "coverage_shell", "source": "model",
                    "label_value": {"coverage": COVERAGE_CLASSES[int(category)],
                                    "confidence": float(probs[int(category)]),
                                    "provenance": {"source": source, "actual_model": "coverage-gnn-numpy",
                                                   "dataset_sha256": digest, "validated": False}}}
                   for row, category, probs in zip(groups["holdout"], predicted, probabilities, strict=True)]
    version_hash = hashlib.sha256(f"{digest}:local-coverage-v1:seed={seed}".encode()).hexdigest()
    output = Path(output_dir).expanduser().resolve() / f"coverage-{version_hash[:16]}"
    _atomic_json(output / "checkpoint.json", model.to_checkpoint())
    _atomic_json(output / "predictions.json", predictions)
    _atomic_json(output / "report.json", report)  # report is the completed-candidate marker
    return {**report, "checkpoint_path": str(output / "checkpoint.json"),
            "report_path": str(output / "report.json"), "predictions_path": str(output / "predictions.json")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--features", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        report = train_candidate(read_jsonl(args.labels), read_jsonl(args.features), args.output, seed=args.seed)
    except (TrainingDataError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": getattr(exc, "status", "invalid_data"), "reason": str(exc)}))
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
