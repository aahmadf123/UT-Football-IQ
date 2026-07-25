"""Tests for the calibrated-uncertainty active-learning policy (#145 / #146).

Two layers:

* Pure unit tests for ``app.active_learning`` — the prioritisation math, the
  uncalibrated/missing fallbacks, and the "never present an uncalibrated score
  as a confidence" guarantee.
* Endpoint tests for the MLOps active-learning queue list response, which makes
  that guarantee at the coach-facing boundary, using the shared
  mocked-AsyncSession pattern.

The nightly populator's priority computation is exercised through the pure
policy tests below — ``run_nightly_active_learning`` simply calls
``annotation_priority(signal_from_label_value(...))``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.active_learning import (
    UncertaintySignal,
    annotation_priority,
    basis_for,
    parse_uncertainty,
    signal_from_label_value,
)
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    ActiveLearningQueueItem,
    ActiveLearningReason,
    ActiveLearningStatus,
    User,
    UserRole,
)
from fastapi.testclient import TestClient

# ── Pure policy: parse_uncertainty ────────────────────────────────────────────


def test_parse_uncertainty_calibrated() -> None:
    sig = parse_uncertainty(
        {"calibrated": True, "method": "temperature", "confidence": 0.82, "entropy": 0.2}
    )
    assert sig is not None
    assert sig.calibrated is True
    assert sig.method == "temperature"
    assert sig.confidence == pytest.approx(0.82)
    assert sig.entropy == pytest.approx(0.2)
    assert sig.raw_score is None


def test_parse_uncertainty_uncalibrated_demotes_confidence() -> None:
    # An uncalibrated payload that still carries a raw "confidence" must NOT
    # surface it as a confidence — it becomes a ranking-only raw_score.
    sig = parse_uncertainty(
        {"calibrated": False, "method": "uncalibrated", "confidence": 0.91, "entropy": 0.1}
    )
    assert sig is not None
    assert sig.calibrated is False
    assert sig.confidence is None
    assert sig.raw_score == pytest.approx(0.91)
    assert sig.entropy == pytest.approx(0.1)


@pytest.mark.parametrize("payload", [None, {}, 123, "x", [], {"method": "none"}])
def test_parse_uncertainty_missing_returns_none(payload: Any) -> None:
    assert parse_uncertainty(payload) is None


def test_parse_uncertainty_clamps_out_of_range() -> None:
    sig = parse_uncertainty({"calibrated": True, "confidence": 1.5, "entropy": -0.3})
    assert sig is not None
    assert sig.confidence == pytest.approx(1.0)
    assert sig.entropy == pytest.approx(0.0)


# ── Pure policy: signal_from_label_value ──────────────────────────────────────


def test_signal_from_label_value_prefers_structured() -> None:
    sig = signal_from_label_value(
        {
            "value": "cover_3",
            "confidence": 0.4,
            "uncertainty": {
                "calibrated": True,
                "method": "platt",
                "confidence": 0.7,
                "entropy": 0.3,
            },
        }
    )
    assert sig is not None
    assert sig.calibrated is True
    assert sig.confidence == pytest.approx(0.7)


def test_signal_from_label_value_legacy_confidence_is_uncalibrated() -> None:
    sig = signal_from_label_value({"value": "trips", "confidence": 0.3})
    assert sig is not None
    assert sig.calibrated is False
    assert sig.confidence is None
    assert sig.raw_score == pytest.approx(0.3)


@pytest.mark.parametrize("value", [None, 7, "x", {}, {"value": "trips"}])
def test_signal_from_label_value_no_signal_returns_none(value: Any) -> None:
    assert signal_from_label_value(value) is None


# ── Pure policy: annotation_priority ──────────────────────────────────────────


def test_priority_missing_uses_fallback() -> None:
    decision = annotation_priority(None, missing_priority=0.42)
    assert decision.priority == pytest.approx(0.42)
    assert decision.reason == ActiveLearningReason.uncertainty_sampling
    assert decision.basis == "missing"


def test_priority_calibrated_orders_by_entropy() -> None:
    low = annotation_priority(
        UncertaintySignal(True, "platt", confidence=0.5, entropy=0.1), entropy_weight=0.5
    )
    high = annotation_priority(
        UncertaintySignal(True, "platt", confidence=0.5, entropy=0.9), entropy_weight=0.5
    )
    assert high.priority > low.priority
    assert high.basis == "calibrated"
    assert high.reason == ActiveLearningReason.low_confidence


def test_priority_calibrated_confident_is_low() -> None:
    decision = annotation_priority(
        UncertaintySignal(True, "platt", confidence=0.97, entropy=0.05), entropy_weight=0.5
    )
    assert decision.priority < 0.1


def test_priority_uncalibrated_ranks_on_raw_and_hides_confidence() -> None:
    sig = signal_from_label_value({"confidence": 0.3})
    assert sig is not None and sig.confidence is None  # never a confidence
    decision = annotation_priority(sig)
    # Ranks like the legacy 1 - raw_score, so a low raw score is high priority.
    assert decision.priority == pytest.approx(0.7)
    assert decision.reason == ActiveLearningReason.low_confidence
    assert decision.basis == "uncalibrated"


def test_priority_uncalibrated_floor_when_no_signal() -> None:
    sig = UncertaintySignal(False, "none", confidence=None, entropy=None, raw_score=None)
    decision = annotation_priority(sig, uncalibrated_priority=0.55)
    assert decision.priority == pytest.approx(0.55)
    assert decision.reason == ActiveLearningReason.uncertainty_sampling
    assert decision.basis == "uncalibrated"


def test_priority_calibrated_uses_policy_blend() -> None:
    # 0.5 * entropy(0.8) + 0.5 * (1 - confidence(0.3)) = 0.75
    sig = signal_from_label_value(
        {
            "uncertainty": {
                "calibrated": True,
                "method": "temperature",
                "confidence": 0.3,
                "entropy": 0.8,
            }
        }
    )
    decision = annotation_priority(sig, entropy_weight=0.5)
    assert decision.priority == pytest.approx(0.75)
    assert decision.basis == "calibrated"


def test_priority_is_clamped() -> None:
    decision = annotation_priority(None, missing_priority=5.0)
    assert decision.priority == pytest.approx(1.0)


def test_as_payload_round_trips() -> None:
    sig = UncertaintySignal(True, "temperature", confidence=0.8, entropy=0.25, raw_score=None)
    reparsed = parse_uncertainty(sig.as_payload())
    assert reparsed == sig


def test_basis_for() -> None:
    assert basis_for(None) == "missing"
    assert basis_for(UncertaintySignal(True, "platt", 0.8, 0.2)) == "calibrated"
    assert basis_for(UncertaintySignal(False, "none", None, None, 0.3)) == "uncalibrated"


# ── Endpoint helpers ──────────────────────────────────────────────────────────


def _make_user(role: UserRole = UserRole.analyst) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _override_auth(role: UserRole = UserRole.analyst) -> User:
    user = _make_user(role)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


def _make_queue_item(**overrides: Any) -> ActiveLearningQueueItem:
    item = MagicMock(spec=ActiveLearningQueueItem)
    item.id = overrides.get("id", uuid.uuid4())
    item.clip_id = overrides.get("clip_id", uuid.uuid4())
    item.label_type = overrides.get("label_type", "coverage_shell")
    item.queue_reason = overrides.get("queue_reason", ActiveLearningReason.low_confidence)
    item.priority_score = overrides.get("priority_score", 0.7)
    item.model_confidence = overrides.get("model_confidence", 0.3)
    item.source_model_version_id = overrides.get("source_model_version_id", None)
    item.status = overrides.get("status", ActiveLearningStatus.queued)
    item.metadata_ = overrides.get("metadata_", None)
    item.created_at = overrides.get("created_at", datetime.now(UTC))
    return item


# ── Endpoint: list response is calibration-honest ─────────────────────────────


def test_queue_list_hides_uncalibrated_confidence() -> None:
    rows = [
        _make_queue_item(
            label_type="coverage_shell",
            priority_score=0.9,
            metadata_={
                "uncertainty": {
                    "calibrated": False,
                    "method": "uncalibrated",
                    "confidence": 0.92,
                    "entropy": 0.1,
                    "raw_score": 0.1,
                }
            },
        ),
        _make_queue_item(
            label_type="offensive_formation",
            priority_score=0.4,
            metadata_={
                "uncertainty": {
                    "calibrated": True,
                    "method": "platt",
                    "confidence": 0.6,
                    "entropy": 0.5,
                }
            },
        ),
    ]

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/mlops/active-learning/queue")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    uncal = body[0]
    assert uncal["calibrated"] is False
    assert uncal["basis"] == "uncalibrated"
    # The uncalibrated raw score (0.92) is NEVER surfaced as a confidence.
    assert uncal["model_confidence"] is None

    cal = body[1]
    assert cal["calibrated"] is True
    assert cal["basis"] == "calibrated"
    assert cal["model_confidence"] == pytest.approx(0.6)


def test_queue_list_missing_metadata_is_safe() -> None:
    rows = [_make_queue_item(metadata_=None)]

    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)
        yield session

    _override_auth()
    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/mlops/active-learning/queue")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    item = resp.json()[0]
    assert item["basis"] == "missing"
    assert item["calibrated"] is False
    assert item["model_confidence"] is None
    assert item["entropy"] is None
