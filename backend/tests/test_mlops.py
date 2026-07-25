"""Tests for the MLOps router — model registry, dataset versioning, AL queue."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.models import (
    ActiveLearningReason,
    ActiveLearningStatus,
    ModelStage,
)
from app.routers.mlops import (
    ModelVersionCreate,
    ModelVersionResponse,
    _confidence,
    _normalized_value,
)
from fastapi.testclient import TestClient

# ── Unit tests for pure helpers ───────────────────────────────────────────────


def test_confidence_returns_float_for_numeric() -> None:
    assert _confidence({"confidence": 0.9}) == pytest.approx(0.9)
    assert _confidence({"confidence": 1}) == pytest.approx(1.0)


def test_confidence_returns_none_for_missing_key() -> None:
    assert _confidence({}) is None
    assert _confidence(None) is None


def test_confidence_returns_none_for_non_numeric() -> None:
    assert _confidence({"confidence": "high"}) is None


def test_normalized_value_is_stable() -> None:
    v1 = _normalized_value({"b": 2, "a": 1})
    v2 = _normalized_value({"a": 1, "b": 2})
    assert v1 == v2


def test_normalized_value_differs_for_different_dicts() -> None:
    assert _normalized_value({"a": 1}) != _normalized_value({"a": 2})


# ── Pydantic schema validation ────────────────────────────────────────────────


def test_model_version_create_requires_name_and_version() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ModelVersionCreate(
            model_name="",  # min_length=1 violated
            version="v1",
            model_type="YOLO",
        )


def test_model_version_create_valid() -> None:
    m = ModelVersionCreate(
        model_name="player_detector",
        version="v2.1",
        model_type="YOLO",
        metrics={"accuracy": 0.92},
    )
    assert m.model_name == "player_detector"
    assert m.metrics == {"accuracy": 0.92}


def test_model_version_response_serializes_stage() -> None:
    resp = ModelVersionResponse(
        id=uuid.uuid4(),
        model_name="route_classifier",
        version="v1",
        model_type="transformer",
        artifact_uri=None,
        training_dataset_id=None,
        metrics=None,
        promoted_stage=ModelStage.experimental,
    )
    assert resp.promoted_stage == ModelStage.experimental


# ── Route authentication guard tests (no DB needed) ──────────────────────────


def test_list_models_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/mlops/models")
    assert response.status_code == 401


def test_get_model_requires_auth(client: TestClient) -> None:
    response = client.get(f"/api/v1/mlops/models/{uuid.uuid4()}")
    assert response.status_code == 401


def test_create_model_requires_auth(client: TestClient) -> None:
    response = client.post(
        "/api/v1/mlops/models",
        json={
            "model_name": "test",
            "version": "v1",
            "model_type": "YOLO",
        },
    )
    assert response.status_code == 401


def test_promote_model_requires_auth(client: TestClient) -> None:
    response = client.post(
        f"/api/v1/mlops/models/{uuid.uuid4()}/promote",
        json={"stage": "staging"},
    )
    assert response.status_code == 401


def test_list_datasets_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/mlops/datasets")
    assert response.status_code == 401


def test_list_al_queue_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/mlops/active-learning/queue")
    assert response.status_code == 401


def test_nightly_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/mlops/active-learning/nightly")
    assert response.status_code == 401


# ── Mocked DB tests for business logic ───────────────────────────────────────


def _make_mock_model(
    *,
    metrics: dict[str, Any] | None = None,
    stage: ModelStage = ModelStage.experimental,
) -> MagicMock:
    """Return a MagicMock that looks like a ModelVersion ORM row."""
    m = MagicMock()
    m.id = uuid.uuid4()
    m.model_name = "player_detector"
    m.version = "v1"
    m.model_type = "YOLO"
    m.artifact_uri = "s3://artifacts/v1.pt"
    m.training_dataset_id = None
    m.metrics = metrics
    m.promoted_stage = stage
    m.created_at = datetime.now(tz=UTC)
    return m


@pytest.fixture
def client_with_auth_and_mocked_db(client: TestClient) -> TestClient:  # type: ignore[return]
    """Override DB and auth so we can test business logic in isolation.

    Yields a client where the current user is an admin, and the DB is mocked.
    """
    from app.auth import get_current_user_id
    from app.database import get_db

    async def _fake_user_id() -> str:
        return str(uuid.uuid4())

    mock_session = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()

    async def _fake_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    from app.main import app

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_user_id] = _fake_user_id

    yield client

    app.dependency_overrides.clear()


def test_promote_model_blocked_without_metrics(client_with_auth_and_mocked_db: TestClient) -> None:
    """Promote to staging must be rejected when model has no metrics (app-layer gate)."""
    from app.auth import get_current_user_id
    from app.database import get_db
    from app.deps import get_current_user
    from app.main import app
    from app.models import User, UserRole

    # Build an admin user mock
    admin = MagicMock(spec=User)
    admin.id = uuid.uuid4()
    admin.role = UserRole.admin
    admin.is_active = True

    # A model with no metrics
    model_without_metrics = _make_mock_model(metrics=None)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model_without_metrics

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    async def _fake_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def _fake_user_id() -> str:
        return str(admin.id)

    async def _fake_admin_user() -> User:
        return admin

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_user_id] = _fake_user_id
    app.dependency_overrides[get_current_user] = _fake_admin_user

    client = TestClient(app)
    try:
        resp = client.post(
            f"/api/v1/mlops/models/{model_without_metrics.id}/promote",
            json={"stage": "staging"},
        )
        assert resp.status_code == 422, f"Expected 422 but got {resp.status_code}: {resp.text}"
    finally:
        app.dependency_overrides.clear()


def test_promote_model_to_staging_with_metrics_succeeds(
    client_with_auth_and_mocked_db: TestClient,
) -> None:
    """Promote to staging must succeed when model has metrics."""
    from app.auth import get_current_user_id
    from app.database import get_db
    from app.deps import get_current_user
    from app.main import app
    from app.models import User, UserRole

    admin = MagicMock(spec=User)
    admin.id = uuid.uuid4()
    admin.role = UserRole.admin
    admin.is_active = True

    model_with_metrics = _make_mock_model(metrics={"accuracy": 0.91})
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = model_with_metrics

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    async def _fake_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def _fake_user_id() -> str:
        return str(admin.id)

    async def _fake_admin_user() -> User:
        return admin

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_user_id] = _fake_user_id
    app.dependency_overrides[get_current_user] = _fake_admin_user

    client = TestClient(app)
    try:
        resp = client.post(
            f"/api/v1/mlops/models/{model_with_metrics.id}/promote",
            json={"stage": "staging"},
        )
        assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}: {resp.text}"
        # Ensure the stage was set on the ORM object
        assert model_with_metrics.promoted_stage == ModelStage.staging
    finally:
        app.dependency_overrides.clear()


def test_promote_model_404_for_unknown_id(
    client_with_auth_and_mocked_db: TestClient,
) -> None:
    """Promote returns 404 when the model version does not exist."""
    from app.auth import get_current_user_id
    from app.database import get_db
    from app.deps import get_current_user
    from app.main import app
    from app.models import User, UserRole

    admin = MagicMock(spec=User)
    admin.id = uuid.uuid4()
    admin.role = UserRole.admin
    admin.is_active = True

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.flush = AsyncMock()

    async def _fake_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    async def _fake_user_id() -> str:
        return str(admin.id)

    async def _fake_admin_user() -> User:
        return admin

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_user_id] = _fake_user_id
    app.dependency_overrides[get_current_user] = _fake_admin_user

    client = TestClient(app)
    try:
        resp = client.post(
            f"/api/v1/mlops/models/{uuid.uuid4()}/promote",
            json={"stage": "production"},
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ── Enum coverage tests ───────────────────────────────────────────────────────


def test_active_learning_reason_values() -> None:
    assert set(ActiveLearningReason) == {
        ActiveLearningReason.low_confidence,
        ActiveLearningReason.uncertainty_sampling,
        ActiveLearningReason.regression,
        ActiveLearningReason.hard_negative,
    }


def test_active_learning_status_values() -> None:
    assert set(ActiveLearningStatus) == {
        ActiveLearningStatus.queued,
        ActiveLearningStatus.in_review,
        ActiveLearningStatus.resolved,
    }


def test_model_stage_promotion_order() -> None:
    """Verify that all expected stages are present."""
    assert set(ModelStage) == {
        ModelStage.experimental,
        ModelStage.staging,
        ModelStage.production,
        ModelStage.retired,
    }
