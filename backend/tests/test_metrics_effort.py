"""Metric schema coverage for effort review fields (Issue #142)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.models import Metric
from app.routers.metrics import MetricCreate, MetricResponse


def test_metric_create_accepts_effort_review_fields() -> None:
    payload = MetricCreate(
        clip_id=uuid.uuid4(),
        tracklet_id=uuid.uuid4(),
        metric_name="effort_review_candidate",
        metric_value={"review_state": "needs_coach_review"},
        effort_zscore=-2.5,
        loaf_flag=True,
    )

    assert payload.effort_zscore == -2.5
    assert payload.loaf_flag is True


def test_metric_response_serializes_effort_review_fields() -> None:
    metric = MagicMock(spec=Metric)
    metric.id = uuid.uuid4()
    metric.clip_id = uuid.uuid4()
    metric.tracklet_id = uuid.uuid4()
    metric.metric_name = "effort_review_candidate"
    metric.metric_value = {"review_state": "needs_coach_review"}
    metric.unit = "review"
    metric.is_suppressed = False
    metric.suppression_reason = None
    metric.experimental_flag = True
    metric.analytics_safe = False
    metric.confidence = 0.82
    metric.effort_zscore = -2.5
    metric.loaf_flag = True
    metric.evidence_uri = None
    metric.model_version_id = None
    metric.calibration_version_id = None
    metric.job_id = None
    metric.created_at = datetime(2026, 5, 31, tzinfo=UTC)

    response = MetricResponse.from_orm_metric(metric)

    assert response.effort_zscore == -2.5
    assert response.loaf_flag is True
