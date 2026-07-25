"""Tests for the counterfactual coverage simulator (Issue #141).

Two layers, mirroring the rest of the backend:

* **Engine unit tests** exercise ``app.analytics.counterfactual`` directly (no
  DB): empirical-Bayes estimation, uncertainty bands, the honest
  insufficient-data behaviour, and the label/metric extraction helpers.
* **Router tests** mock DB/auth and cover RBAC (staff-only), the dark-launch
  404, the American-football soccer guard, workload-gated success, the
  low-confidence-input gating, and the insufficient-data path.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from app.analytics import counterfactual as cf
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from fastapi.testclient import TestClient

THRESHOLD = 0.6


def _obs(route: str, coverage: str, yards: float) -> cf.PlayObservation:
    return cf.PlayObservation(route_concept=route, coverage_type=coverage, yards=yards)


def _corpus() -> list[cf.PlayObservation]:
    obs: list[cf.PlayObservation] = []
    obs += [_obs("Go", "Cover 1", y) for y in (18, 22, 25, 20, 16, 24)]
    obs += [_obs("Go", "Cover 3", y) for y in (10, 12, 8, 11, 9, 13)]
    obs += [_obs("Go", "Cover 2 Shell", y) for y in (4, 6, 3, 5, 2, 7)]
    return obs


# ── Engine: estimation & honesty ────────────────────────────────────────────


def test_simulate_returns_ranked_outcomes_with_bands() -> None:
    lookup = cf.CounterfactualLookup.from_observations(_corpus())
    res = lookup.simulate("Go", factual_coverage="Cover 2 Shell", top_n=3)
    assert res.experimental is True
    assert [o.rank for o in res.outcomes] == [1, 2]  # two other coverages exist
    top = res.outcomes[0].to_dict()
    assert top["coverage_type"] == "cover_1"
    assert top["confidence_band"][0] <= top["expected_yards"] <= top["confidence_band"][1]
    assert 0.0 <= top["confidence"] <= cf.CONFIDENCE_CAP
    assert top["experimental"] is True


def test_unseen_route_is_insufficient_not_fabricated() -> None:
    lookup = cf.CounterfactualLookup.from_observations(_corpus())
    res = lookup.simulate("Wheel", factual_coverage="Cover 3")
    assert res.data_sufficiency == cf.SUFF_INSUFFICIENT
    assert res.outcomes == []
    assert lookup.expected_yards("Wheel", "Cover 3").basis == "insufficient"


def test_confidence_capped_even_when_well_sampled() -> None:
    lookup = cf.CounterfactualLookup.from_observations(
        [_obs("Go", "Cover 1", 20) for _ in range(50)]
    )
    assert lookup.expected_yards("Go", "Cover 1").confidence <= cf.CONFIDENCE_CAP


# ── Engine: extraction helpers ──────────────────────────────────────────────


def test_extract_route_and_coverage_from_labels() -> None:
    labels = [
        {"label_type": "play_concept", "label_value": {"concept": "Go"}},
        {"label_type": "coverage_shell", "label_value": {"coverage": "Cover 3"}},
    ]
    assert cf.extract_route_concept(labels) == "Go"
    assert cf.extract_coverage_type(labels) == "Cover 3"


def test_extract_outcome_yards_prefers_explicit_label() -> None:
    labels = [{"label_type": "yards_gained", "label_value": {"yards": 14}}]
    metrics = [{"metric_name": "pre_contact_yards", "metric_value": {"yards": 3}}]
    assert cf.extract_outcome_yards(labels, metrics) == 14.0


def test_extract_outcome_yards_falls_back_to_measured_metrics() -> None:
    labels: list[dict[str, Any]] = []
    metrics = [
        {"metric_name": "pre_contact_yards", "metric_value": {"yards": 4}},
        {"metric_name": "post_contact_yards", "metric_value": {"yards": 5}},
    ]
    assert cf.extract_outcome_yards(labels, metrics) == 9.0


def test_extract_outcome_yards_none_when_absent() -> None:
    assert cf.extract_outcome_yards([], []) is None


def test_build_observations_skips_clips_missing_any_signal() -> None:
    clip_rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    labels_by_clip = {
        "a": [
            {"label_type": "play_concept", "label_value": {"concept": "go"}},
            {"label_type": "coverage_shell", "label_value": {"coverage": "cover 3"}},
        ],
        "b": [  # has route + coverage but no outcome → skipped
            {"label_type": "play_concept", "label_value": {"concept": "go"}},
            {"label_type": "coverage_shell", "label_value": {"coverage": "cover 1"}},
        ],
        "c": [{"label_type": "play_concept", "label_value": {"concept": "go"}}],  # no coverage
    }
    metrics_by_clip = {
        "a": [{"metric_name": "post_contact_yards", "metric_value": {"yards": 11}}],
    }
    obs = cf.build_observations(clip_rows, labels_by_clip, metrics_by_clip)
    assert len(obs) == 1
    assert obs[0].yards == 11.0
    # Raw extracted strings are normalized when added to the lookup.
    normalized = obs[0].normalized()
    assert normalized is not None
    assert normalized.route_concept == "go"
    assert normalized.coverage_type == "cover_3"


# ── Router: helpers ─────────────────────────────────────────────────────────


def _user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _scalar_result(value: Any) -> MagicMock:
    r = MagicMock()
    r.scalar_one.return_value = value
    return r


def _scalars_result(items: list[Any]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value.all.return_value = items
    return r


def _label(clip_id: uuid.UUID, label_type: str, label_value: dict[str, Any]) -> MagicMock:
    m = MagicMock()
    m.clip_id = clip_id
    m.label_type = label_type
    m.label_value = label_value
    return m


def _metric(clip_id: uuid.UUID, name: str, value: dict[str, Any]) -> MagicMock:
    m = MagicMock()
    m.clip_id = clip_id
    m.metric_name = name
    m.metric_value = value
    return m


def _mock_db(results: list[Any]):
    # The workload dependency runs two scalar_one() queries before the router's
    # own clip/label/metric queries, so callers prepend two 0-count results.
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=results)
        yield session

    return _db


def _gated_ok() -> list[Any]:
    return [_scalar_result(0), _scalar_result(0)]  # queued, running → healthy


# ── Router: RBAC / flag / guard ─────────────────────────────────────────────


def test_player_is_forbidden() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.player)
    app.dependency_overrides[get_db] = _mock_db(_gated_ok())
    with TestClient(app) as c:
        resp = c.post("/api/v1/counterfactuals", json={"route_concept": "go"})
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_dark_launch_404(monkeypatch: Any) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "counterfactual_simulator_enabled", False)
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db(_gated_ok())
    with TestClient(app) as c:
        resp = c.post("/api/v1/counterfactuals", json={"route_concept": "go"})
    app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_soccer_terms_rejected() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db(_gated_ok())
    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/counterfactuals",
            json={"route_concept": "go", "factual_coverage": "offside trap pitch"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "soccer_resource_rejected"


# ── Router: simulate ────────────────────────────────────────────────────────


def _seed_clips() -> tuple[list[MagicMock], list[MagicMock], list[MagicMock]]:
    clips: list[MagicMock] = []
    labels: list[MagicMock] = []
    metrics: list[MagicMock] = []
    plan = [("cover 3", 10)] * 6 + [("cover 1", 20)] * 6
    for coverage, yards in plan:
        cid = uuid.uuid4()
        clip = MagicMock()
        clip.id = cid
        clips.append(clip)
        labels.append(_label(cid, "play_concept", {"concept": "go"}))
        labels.append(_label(cid, "coverage_shell", {"coverage": coverage}))
        metrics.append(_metric(cid, "post_contact_yards", {"yards": yards}))
    return clips, labels, metrics


def test_simulate_happy_path() -> None:
    clips, labels, metrics = _seed_clips()
    results = _gated_ok() + [
        _scalars_result(clips),
        _scalars_result(labels),
        _scalars_result(metrics),
    ]
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db(results)
    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/counterfactuals",
            json={"route_concept": "go", "factual_coverage": "cover 3", "factual_yards": 10},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert data["experimental"] is True
    assert data["trusted_for_coaching"] is False
    assert data["coach_language"] == "experimental_only"
    assert data["corpus_size"] == 12
    assert data["data_sufficiency"] == cf.SUFF_SUFFICIENT
    # cover_1 is the lone counterfactual and clearly beats the cover_3 baseline.
    assert len(data["outcomes"]) == 1
    out = data["outcomes"][0]
    assert out["coverage_type"] == "cover_1"
    assert out["expected_yards"] > data["factual"]["expected_yards"]
    assert out["delta_vs_factual"] > 0


def test_low_confidence_inputs_block_trusted_language() -> None:
    clips, labels, metrics = _seed_clips()
    results = _gated_ok() + [
        _scalars_result(clips),
        _scalars_result(labels),
        _scalars_result(metrics),
    ]
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.analyst)
    app.dependency_overrides[get_db] = _mock_db(results)
    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/counterfactuals",
            json={
                "route_concept": "go",
                "factual_coverage": "cover 3",
                "input_quality": {
                    "identity_confidence": 0.2,
                    "tracking_confidence": 0.3,
                    "calibration_confirmed": False,
                },
            },
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert data["trusted_for_coaching"] is False
    for reason in ("identity", "tracking", "calibration"):
        assert reason in data["low_confidence_inputs"]


def test_insufficient_data_is_honest_not_mocked() -> None:
    results = _gated_ok() + [_scalars_result([])]  # no clips → no further queries
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db(results)
    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/counterfactuals",
            json={"route_concept": "go", "factual_coverage": "cover 3"},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert data["corpus_size"] == 0
    assert data["data_sufficiency"] == cf.SUFF_INSUFFICIENT
    assert data["outcomes"] == []
    assert "sparse_sample" in data["low_confidence_inputs"]
