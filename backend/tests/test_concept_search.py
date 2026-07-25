"""Tests for zero-shot concept search (Issue #144).

The grounded metadata query and the pgvector expansion both run in Postgres, so
these tests follow ``test_search.py``: they exercise the pure lexicon / predicate
/ centroid logic directly and mock the DB session for the router-level paths.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.concept_lexicon import (
    CONCEPTS,
    ConceptCategory,
    detect_soccer_terms,
    group_by_category,
    parse_query,
)
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from app.routers.concept_search import (
    _centroid,
    _concept_predicate,
    _grounded_predicate,
)
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql


def _make_user(role: UserRole = UserRole.coach) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _compile(expr: Any) -> str:
    return str(expr.compile(dialect=postgresql.dialect())).lower()


# ── Lexicon parsing ───────────────────────────────────────────────────────────


def test_parse_query_finds_play_concept() -> None:
    matches = parse_query("find me all the mesh concepts")
    ids = {m.concept.concept_id for m in matches}
    assert "mesh" in ids


def test_parse_query_multi_concept_across_categories() -> None:
    matches = parse_query("show every cover 3 trips look")
    ids = {m.concept.concept_id for m in matches}
    assert "cover_3" in ids
    assert "trips" in ids
    cats = {m.concept.category for m in matches}
    assert ConceptCategory.COVERAGE in cats
    assert ConceptCategory.FORMATION in cats


def test_parse_query_prefers_specific_coverage_over_bare_zone() -> None:
    # "cover 3" must resolve to cover_3, not collapse to a generic zone match.
    matches = parse_query("cover 3")
    ids = {m.concept.concept_id for m in matches}
    assert "cover_3" in ids


def test_parse_query_power_run() -> None:
    matches = parse_query("power right with a pulling guard")
    assert "power" in {m.concept.concept_id for m in matches}


def test_parse_query_personnel_requires_explicit_phrase() -> None:
    # A bare year-like number must not trigger a personnel match.
    assert parse_query("the 2011 season opener") == []
    matches = parse_query("11 personnel empty")
    ids = {m.concept.concept_id for m in matches}
    assert "personnel_11" in ids
    assert "empty" in ids


def test_parse_query_returns_empty_for_unrecognized() -> None:
    assert parse_query("xyzzy nonsense words") == []
    assert parse_query("") == []


def test_detect_soccer_terms_flags_association_football() -> None:
    assert detect_soccer_terms("4-4-2 high press and xg") != []
    assert detect_soccer_terms("trips right cover 3") == []


def test_lexicon_is_american_football_only() -> None:
    # No soccer vocabulary leaked into the lexicon aliases.
    blob = " ".join(a for c in CONCEPTS for a in c.aliases).lower()
    for soccer in ("offside", "midfielder", "striker", "4-4-2", "xg"):
        assert soccer not in blob


def test_group_by_category_buckets_matches() -> None:
    matches = parse_query("cover 3 trips power")
    grouped = group_by_category(matches)
    assert ConceptCategory.COVERAGE in grouped
    assert ConceptCategory.FORMATION in grouped
    assert ConceptCategory.CONCEPT in grouped


# ── Predicate building ────────────────────────────────────────────────────────


def test_concept_predicate_personnel_targets_column() -> None:
    concept = next(c for c in CONCEPTS if c.concept_id == "personnel_11")
    sql = _compile(_concept_predicate(concept))
    assert "personnel_grouping" in sql
    assert "label_data" not in sql


def test_concept_predicate_field_zone_targets_column() -> None:
    concept = next(c for c in CONCEPTS if c.concept_id == "red_zone")
    sql = _compile(_concept_predicate(concept))
    assert "field_zone" in sql


def test_concept_predicate_coverage_uses_label_data_path_and_fallback() -> None:
    concept = next(c for c in CONCEPTS if c.concept_id == "cover_3")
    sql = _compile(_concept_predicate(concept))
    assert "label_data" in sql
    assert "ilike" in sql


def test_grounded_predicate_ands_across_categories() -> None:
    matches = parse_query("cover 3 trips")
    sql = _compile(_grounded_predicate(matches))
    # Two categories → an AND of two OR-groups; both label_data paths present.
    assert " and " in sql


# ── Centroid ──────────────────────────────────────────────────────────────────


def test_centroid_normalizes() -> None:
    out = _centroid([[3.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    assert out is not None
    assert abs((sum(x * x for x in out)) ** 0.5 - 1.0) < 1e-6
    assert out[0] == 1.0


def test_centroid_handles_empty_and_mismatched() -> None:
    assert _centroid([]) is None
    assert _centroid([[]]) is None
    assert _centroid([[1.0, 2.0], [1.0]]) is None
    assert _centroid([[0.0, 0.0]]) is None  # zero vector → no direction


# ── Router behaviour with a mocked DB ─────────────────────────────────────────


def _override_user(role: UserRole = UserRole.coach) -> None:
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)


def _mock_db(execute_results: list[Any]) -> Any:
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=execute_results)
        yield session

    return _db


def test_concept_search_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/concept-search", params={"q": "mesh"})
    assert resp.status_code == 401


def test_concept_search_blocks_player() -> None:
    _override_user(UserRole.player)
    app.dependency_overrides[get_db] = _mock_db([])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/concept-search", params={"q": "mesh"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_concept_search_no_match_returns_reason() -> None:
    _override_user()
    app.dependency_overrides[get_db] = _mock_db([])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/concept-search", params={"q": "xyzzy nonsense"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["matched_concepts"] == []
    assert body["approximate"] is True
    assert "American-football concept" in body["reason"]


def test_concept_search_rejects_soccer_query() -> None:
    _override_user()
    app.dependency_overrides[get_db] = _mock_db([])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/concept-search", params={"q": "4-4-2 high press xg"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert "soccer" in body["reason"].lower()


def test_concept_search_metadata_only_when_no_embedding_model() -> None:
    clip_id = uuid.uuid4()
    grounded = MagicMock()
    grounded.all.return_value = [(clip_id, {"formation": {"generic": "trips_right"}}, 0.8)]
    no_model = MagicMock()
    no_model.scalars.return_value.all.return_value = []

    _override_user()
    app.dependency_overrides[get_db] = _mock_db([grounded, no_model])
    try:
        with TestClient(app) as c:
            resp = c.get("/api/v1/concept-search", params={"q": "trips"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert "trips" in {m["concept_id"] for m in body["matched_concepts"]}
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["source"] == "metadata"
    assert result["is_experimental"] is False
    assert body["approximate"] is True
    assert body["experimental"] is False
    assert body["model_version_label"] is None
