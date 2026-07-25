"""Tests for the play-embeddings persistence router (Issue #8)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import (
    PLAY_EMBEDDING_CLIP_DIM,
    PLAY_EMBEDDING_DIM,
    PLAY_EMBEDDING_STRUCTURED_DIM,
    PLAY_EMBEDDING_VISUAL_DIM,
    Clip,
    ModelVersion,
    PlayEmbedding,
    User,
    UserRole,
)
from app.routers.embeddings import PlayEmbeddingCreate, PlayEmbeddingResponse
from fastapi.testclient import TestClient


def _make_user(role: UserRole = UserRole.analyst) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _scalar_one_result(value: Any) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    r.scalar_one.return_value = value
    return r


def _mock_db_gen_for_create(
    *,
    clip: Any | None,
    model_version: Any | None,
    inserted: PlayEmbedding,
):
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _scalar_one_result(clip),
                _scalar_one_result(model_version),
                _scalar_one_result(inserted),
            ]
        )
        yield session

    return _db


def _embedding_row(clip_id: uuid.UUID, mv_id: uuid.UUID) -> MagicMock:
    e = MagicMock(spec=PlayEmbedding)
    e.id = uuid.uuid4()
    e.clip_id = clip_id
    e.chunk_kind = "play"
    e.snap_anchor = True
    e.model_version_id = mv_id
    e.calibration_version_id = None
    e.used_sam_masks = False
    e.embedding_confidence = 0.82
    e.is_experimental = True
    e.source_label_ids = []
    e.vector = [0.0] * PLAY_EMBEDDING_DIM
    e.clip_vector = None
    e.created_at = MagicMock()
    e.created_at.isoformat.return_value = "2026-05-26T00:00:00+00:00"
    return e


# ── Schema validation ────────────────────────────────────────────────────────


def test_play_embedding_create_rejects_short_vector() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PlayEmbeddingCreate(
            clip_id=uuid.uuid4(),
            model_version_id=uuid.uuid4(),
            vector=[0.0] * 10,
        )


def test_play_embedding_create_accepts_full_vector_with_subvectors() -> None:
    payload = PlayEmbeddingCreate(
        clip_id=uuid.uuid4(),
        model_version_id=uuid.uuid4(),
        vector=[0.0] * PLAY_EMBEDDING_DIM,
        visual_vector=[0.0] * PLAY_EMBEDDING_VISUAL_DIM,
        structured_vector=[0.0] * PLAY_EMBEDDING_STRUCTURED_DIM,
        snap_anchor=False,
        used_sam_masks=True,
        embedding_confidence=0.75,
    )
    assert payload.snap_anchor is False
    assert payload.used_sam_masks is True


def test_play_embedding_create_rejects_wrong_sub_vector_dims() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PlayEmbeddingCreate(
            clip_id=uuid.uuid4(),
            model_version_id=uuid.uuid4(),
            vector=[0.0] * PLAY_EMBEDDING_DIM,
            visual_vector=[0.0] * 10,
        )


def test_play_embedding_create_accepts_clip_vector() -> None:
    payload = PlayEmbeddingCreate(
        clip_id=uuid.uuid4(),
        model_version_id=uuid.uuid4(),
        vector=[0.0] * PLAY_EMBEDDING_DIM,
        clip_vector=[0.0] * PLAY_EMBEDDING_CLIP_DIM,
    )
    assert payload.clip_vector is not None
    assert len(payload.clip_vector) == PLAY_EMBEDDING_CLIP_DIM


def test_play_embedding_create_rejects_wrong_clip_vector_dim() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        PlayEmbeddingCreate(
            clip_id=uuid.uuid4(),
            model_version_id=uuid.uuid4(),
            vector=[0.0] * PLAY_EMBEDDING_DIM,
            clip_vector=[0.0] * 256,
        )


def test_play_embedding_create_clip_vector_optional() -> None:
    payload = PlayEmbeddingCreate(
        clip_id=uuid.uuid4(),
        model_version_id=uuid.uuid4(),
        vector=[0.0] * PLAY_EMBEDDING_DIM,
    )
    assert payload.clip_vector is None


def test_play_embedding_response_reports_has_clip_vector() -> None:
    clip_id = uuid.uuid4()
    mv_id = uuid.uuid4()
    row = _embedding_row(clip_id, mv_id)
    row.clip_vector = None
    assert PlayEmbeddingResponse.from_orm_embedding(row).has_clip_vector is False
    row.clip_vector = [0.0] * PLAY_EMBEDDING_CLIP_DIM
    assert PlayEmbeddingResponse.from_orm_embedding(row).has_clip_vector is True


def test_play_embedding_response_serialises_lineage() -> None:
    clip_id = uuid.uuid4()
    mv_id = uuid.uuid4()
    row = _embedding_row(clip_id, mv_id)
    row.source_label_ids = [uuid.uuid4(), uuid.uuid4()]
    resp = PlayEmbeddingResponse.from_orm_embedding(row)
    assert resp.clip_id == clip_id
    assert resp.model_version_id == mv_id
    assert resp.vector_dim == PLAY_EMBEDDING_DIM
    assert len(resp.source_label_ids) == 2
    assert all(isinstance(s, str) for s in resp.source_label_ids)


# ── Endpoint behaviour ───────────────────────────────────────────────────────


def test_create_play_embedding_404s_when_clip_missing() -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_gen_for_create(
        clip=None,
        model_version=None,
        inserted=MagicMock(spec=PlayEmbedding),
    )
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/embeddings/play",
                json={
                    "clip_id": str(uuid.uuid4()),
                    "model_version_id": str(uuid.uuid4()),
                    "vector": [0.0] * PLAY_EMBEDDING_DIM,
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404
    assert "Clip not found" in resp.json()["detail"]


def test_create_play_embedding_404s_when_model_version_missing() -> None:
    fake_clip = MagicMock(spec=Clip)
    fake_clip.id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_gen_for_create(
        clip=fake_clip,
        model_version=None,
        inserted=MagicMock(spec=PlayEmbedding),
    )
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/embeddings/play",
                json={
                    "clip_id": str(uuid.uuid4()),
                    "model_version_id": str(uuid.uuid4()),
                    "vector": [0.0] * PLAY_EMBEDDING_DIM,
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404
    assert "Model version" in resp.json()["detail"]


def test_create_play_embedding_persists_row_and_returns_response() -> None:
    clip_id = uuid.uuid4()
    mv_id = uuid.uuid4()
    fake_clip = MagicMock(spec=Clip)
    fake_clip.id = clip_id
    fake_mv = MagicMock(spec=ModelVersion)
    fake_mv.id = mv_id
    inserted = _embedding_row(clip_id, mv_id)

    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_db] = _mock_db_gen_for_create(
        clip=fake_clip,
        model_version=fake_mv,
        inserted=inserted,
    )
    try:
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/embeddings/play",
                json={
                    "clip_id": str(clip_id),
                    "model_version_id": str(mv_id),
                    "vector": [0.0] * PLAY_EMBEDDING_DIM,
                    "visual_vector": [0.0] * PLAY_EMBEDDING_VISUAL_DIM,
                    "structured_vector": [0.0] * PLAY_EMBEDDING_STRUCTURED_DIM,
                    "snap_anchor": True,
                    "embedding_confidence": 0.82,
                    "is_experimental": True,
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 201
    body = resp.json()
    assert body["clip_id"] == str(clip_id)
    assert body["model_version_id"] == str(mv_id)
    assert body["vector_dim"] == PLAY_EMBEDDING_DIM
    assert body["is_experimental"] is True


def test_create_play_embedding_requires_staff_role(client) -> None:
    resp = client.post(
        "/api/v1/embeddings/play",
        json={
            "clip_id": str(uuid.uuid4()),
            "model_version_id": str(uuid.uuid4()),
            "vector": [0.0] * PLAY_EMBEDDING_DIM,
        },
    )
    assert resp.status_code == 401
