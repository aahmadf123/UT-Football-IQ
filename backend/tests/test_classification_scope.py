"""Capture-slice release mechanics on numerical fixtures; no football validation."""

# ruff: noqa: F811
import uuid
from types import SimpleNamespace

import pytest
from app.models import Clip, Label
from app.services.label_publication import (
    classification_publication_approvals,
    label_scope_approved,
)
from app.services.learning import _classification_scope, normalized_label
from test_label_publication import fixture
from test_platform_overhaul import actor, db, recording  # noqa: F401


@pytest.mark.parametrize("case", ["valid", "unknown", "small_slices", "failed_slice"])
async def test_actual_clip_regimes_define_independent_baseline_slices(
    db, monkeypatch, tmp_path, case
):
    _, _, video, _, model, _, _, _ = await fixture(db, monkeypatch, tmp_path)
    samples, predictions = [], []
    count = 40 if case == "failed_slice" else 20
    for index in range(count):
        regime = "fixed_sideline"
        if case == "unknown":
            regime = None
        if case == "small_slices" and index >= 10:
            regime = "fixed_endzone"
        if case == "failed_slice" and index >= 20:
            regime = "fixed_endzone"
        clip = Clip(
            id=uuid.uuid4(), video_id=video.id, start_time=0, end_time=4, capture_regime=regime
        )
        db.add(clip)
        truth = "cover_1" if index % 2 else "cover_3"
        samples.append(
            {
                "clip_id": str(clip.id),
                "tracklet_id": None,
                "label_type": "coverage_shell",
                "truth": {"coverage": truth},
            }
        )
        predicted = "cover_1" if case == "failed_slice" and index >= 20 else truth
        predictions.append(
            Label(
                id=uuid.uuid4(),
                clip_id=clip.id,
                label_type="coverage_shell",
                model_version_id=model.id,
                source="model",
                label_value={"coverage": predicted, "provenance": {"source": "real"}},
            )
        )
    await db.flush()
    scope = await _classification_scope(
        db,
        model,
        samples,
        predictions,
        {"majority_class": normalized_label({"coverage": "cover_1"}, "coverage_shell")},
    )
    assert scope["passed"] is (case == "valid")
    if case == "unknown":
        assert scope["unknown_sample_count"] == 20 and scope["capture_regimes"] == []
    else:
        for item in scope["slices"].values():
            assert item["baseline_accuracy"] == 0.5
    if case == "valid":
        approved = classification_publication_approvals(
            scope, "actual-fixture-evaluation", model.metrics["artifact_sha256"]
        )
        assert approved["coverage_shell"]["capture_regimes"] == ["fixed_sideline"]
    else:
        with pytest.raises(ValueError, match="did not pass"):
            classification_publication_approvals(scope, "fixture", "a" * 64)


@pytest.mark.parametrize(
    "metadata,proof",
    [
        ([], {}),
        ({"label_publication_approvals": []}, {}),
        ({}, []),
        ({"label_publication_approvals": {"coverage_shell": []}}, {}),
    ],
)
def test_malformed_scope_metadata_never_qualifies(metadata, proof):
    model = SimpleNamespace(metrics=metadata)
    assert (
        label_scope_approved(
            model,
            SimpleNamespace(label_type="coverage_shell"),
            SimpleNamespace(capture_regime="fixed_sideline"),
            proof,
        )
        is False
    )
