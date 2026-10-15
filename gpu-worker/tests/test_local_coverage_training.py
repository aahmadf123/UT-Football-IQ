"""CPU learning contract with explicitly synthetic numerical fixtures."""

from __future__ import annotations

import json

import numpy as np
import pytest

from pipeline.coverage.gnn_classifier import CoverageGNN
from pipeline.coverage.train_coverage_gnn import graph_from_dict
from training.local_coverage import TrainingDataError, train_candidate


def _examples():
    labels, features = [], []
    for split, count in {"train": 30, "calibration": 10, "holdout": 20}.items():
        for i in range(count):
            cid = f"{split}-{i}"
            category = "cover_1" if i % 2 else "cover_3"
            nodes = [[50 + j, float(j), .2, 12. if i % 2 else 2., 3., 0., 0.]
                     for j in range(5)]
            graph = {"schema_version": 1, "node_ids": [f"p{j}" for j in range(5)],
                     "node_features": nodes, "edge_index": [[], []], "edge_features": []}
            labels.append({"clip_id": cid, "label_id": f"truth-{cid}", "source": "human",
                           "annotated_by": "fixture-reviewer", "label_type": "coverage_tag",
                           "label_value": {"coverage": category}})
            features.append({"clip_id": cid, "session_id": f"fixture-session-{split}",
                             "split": split, "source": "synthetic_test", "analytics_safe": True,
                             "graph": graph})
    return labels, features


def test_cpu_candidate_fits_and_persists_with_separate_holdout(tmp_path):
    labels, features = _examples()
    result = train_candidate(labels, features, tmp_path, allow_synthetic_test=True)
    assert result["status"] == "candidate"
    assert result["device"] == "cpu"
    assert result["accuracy"] > .9
    assert result["macro_f1"] > .9
    assert result["release_gate_passed"] is False  # synthetic math tests are never release evidence
    assert result["promoted"] is False
    checkpoint = json.loads(open(result["checkpoint_path"]).read())
    provenance = checkpoint["provenance"]
    assert provenance["source"] == "synthetic_test"
    assert provenance["validated"] is False
    assert set(provenance["train_clip_ids"]).isdisjoint(provenance["holdout_clip_ids"])
    assert set(provenance["calibration_clip_ids"]).isdisjoint(provenance["holdout_clip_ids"])
    restored = CoverageGNN.from_checkpoint(checkpoint)
    probabilities = restored.predict_proba(graph_from_dict(features[-1]["graph"]))
    assert sum(probabilities.values()) == pytest.approx(1.)
    assert np.isfinite(list(probabilities.values())).all()
    assert len(json.loads(open(result["predictions_path"]).read())) == 20


def test_insufficient_truth_produces_no_weights(tmp_path):
    with pytest.raises(TrainingDataError) as error:
        train_candidate([], [], tmp_path)
    assert error.value.status == "insufficient_data"
    assert list(tmp_path.iterdir()) == []


def test_synthetic_packet_requires_explicit_test_call(tmp_path):
    labels, features = _examples()
    with pytest.raises(TrainingDataError, match="Synthetic"):
        train_candidate(labels, features, tmp_path)


def test_session_leakage_is_rejected(tmp_path):
    labels, features = _examples()
    for packet in features:
        packet["session_id"] = "same-recording-day"
    with pytest.raises(TrainingDataError, match="Session leakage"):
        train_candidate(labels, features, tmp_path, allow_synthetic_test=True)


def test_predictions_cannot_become_truth(tmp_path):
    labels, features = _examples()
    labels[0]["source"] = "model"
    with pytest.raises(TrainingDataError, match="human"):
        train_candidate(labels, features, tmp_path, allow_synthetic_test=True)


def test_same_clip_feature_join_is_required(tmp_path):
    labels, features = _examples()
    features[0]["clip_id"] = "a-different-recording-of-the-same-play-call"
    with pytest.raises(TrainingDataError, match="Same-clip"):
        train_candidate(labels, features, tmp_path, allow_synthetic_test=True)


def test_unmapped_features_and_nonfinite_graph_are_rejected(tmp_path):
    labels, features = _examples()
    features[0]["analytics_safe"] = False
    with pytest.raises(TrainingDataError, match="mapping"):
        train_candidate(labels, features, tmp_path, allow_synthetic_test=True)
    features[0]["analytics_safe"] = True
    features[0]["graph"]["node_features"][0][0] = float("nan")
    with pytest.raises(TrainingDataError, match="finite"):
        train_candidate(labels, features, tmp_path, allow_synthetic_test=True)
