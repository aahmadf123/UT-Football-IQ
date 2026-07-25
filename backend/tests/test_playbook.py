"""Tests for playbook overlays & assignment execution scoring (Issue #15).

Two layers, mirroring how the rest of the backend is tested:

* **Engine unit tests** exercise the pure ``app.analytics.assignment_scoring``
  rules directly (no DB) — grades, confidence/uncertainty, reason codes, and the
  graceful-degradation / identity-safety behaviors the issue requires.
* **Router tests** mock the DB/auth (the dominant pattern here, since pgvector
  columns block ``create_all`` on SQLite) and cover RBAC, validation, the
  American-football-only guard, the dark-launch flag, and overlay degradation.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.analytics import assignment_scoring as sc
from app.database import get_db
from app.deps import get_current_user
from app.main import app
from app.models import User, UserRole
from app.playbook_seed import COVER_3_SKY, FOUR_VERTICALS, SEED_CONCEPTS
from fastapi.testclient import TestClient

THRESHOLD = 0.6


# ── Engine fixtures ─────────────────────────────────────────────────────────


def _def(key: str, rule: dict[str, Any], role: str = "X") -> sc.AssignmentDefinition:
    return sc.AssignmentDefinition(key=key, role=role, intent="i", coaching_point="c", rule=rule)


def _good_sub(
    key: str,
    trajectory: list[sc.TrajectoryPoint],
    *,
    identity_state: str = "known",
    identity_confidence: float = 0.9,
    track_confidence: float = 0.9,
    calibration_confidence: float | None = None,
    concept_source: str = "coach",
    concept_validated: bool = True,
) -> sc.AssignmentSubstrate:
    return sc.AssignmentSubstrate(
        assignment_key=key,
        resolution="ok",
        tracklet_id="t-1",
        identity_state=identity_state,
        identity_confidence=identity_confidence,
        track_confidence=track_confidence,
        calibration_confidence=calibration_confidence,
        has_calibrated_trajectory=any(p.field_y is not None for p in trajectory),
        trajectory=trajectory,
        concept_source=concept_source,
        concept_validated=concept_validated,
    )


def _score(defn: sc.AssignmentDefinition, sub: sc.AssignmentSubstrate) -> sc.AssignmentScoreResult:
    return sc.score_assignment(defn, sub, identity_confidence_threshold=THRESHOLD)


# ── Engine: resolution + identity safety ────────────────────────────────────


def test_no_mapping_is_needs_review() -> None:
    res = _score(_def("k", {"type": "presence"}), sc.AssignmentSubstrate(assignment_key="k"))
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_NO_MAPPING]
    assert res.score is None


def test_tracklet_not_found_is_needs_review() -> None:
    sub = sc.AssignmentSubstrate(
        assignment_key="k", resolution="tracklet_not_found", tracklet_id="missing"
    )
    res = _score(_def("k", {"type": "presence"}), sub)
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_TRACKLET_NOT_FOUND]


def test_low_identity_never_grades_off_assignment() -> None:
    """A clearly-diverging player on a shaky identity is review, not 'off'."""
    traj = [sc.TrajectoryPoint(0.9, 0.9)]  # nowhere near the zone
    rule = {"type": "zone_landmark", "params": {"region": {"x": [0.0, 0.3], "y": [0.0, 0.3]}}}
    # unknown identity
    res = _score(_def("k", rule), _good_sub("k", traj, identity_state="unknown"))
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_LOW_IDENTITY]
    # known but below the confidence threshold
    res2 = _score(_def("k", rule), _good_sub("k", traj, identity_confidence=0.3))
    assert res2.grade == sc.GRADE_REVIEW
    assert res2.reason_codes == [sc.REASON_LOW_IDENTITY]


def test_conflicting_identity_is_needs_review() -> None:
    res = _score(
        _def("k", {"type": "presence"}),
        _good_sub("k", [sc.TrajectoryPoint(0.5, 0.5)], identity_state="conflicting"),
    )
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_LOW_IDENTITY]


# ── Engine: rules ───────────────────────────────────────────────────────────


def test_manual_only_is_needs_review() -> None:
    res = _score(
        _def("qb", {"type": "manual_only"}), _good_sub("qb", [sc.TrajectoryPoint(0.5, 0.5)])
    )
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_MANUAL_ONLY]


def test_unknown_rule_is_needs_review() -> None:
    res = _score(_def("k", {"type": "nope"}), _good_sub("k", [sc.TrajectoryPoint(0.5, 0.5)]))
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_UNKNOWN_RULE]


def test_release_direction_on_and_off() -> None:
    rule = {"type": "release_direction", "params": {"axis": "y", "sign": -1, "min_delta": 0.05}}
    up = [sc.TrajectoryPoint(0.1, 0.9 - 0.2 * i) for i in range(5)]  # drives upfield
    on = _score(_def("k", rule), _good_sub("k", up))
    assert on.grade == sc.GRADE_ON
    assert sc.REASON_MATCHED in on.reason_codes

    flat = [sc.TrajectoryPoint(0.1, 0.5) for _ in range(5)]  # no vertical push
    off = _score(_def("k", rule), _good_sub("k", flat))
    assert off.grade == sc.GRADE_OFF
    assert sc.REASON_DIVERGED in off.reason_codes


def test_release_direction_no_trajectory_degrades() -> None:
    rule = {"type": "release_direction", "params": {"axis": "y", "sign": -1}}
    res = _score(_def("k", rule), _good_sub("k", [sc.TrajectoryPoint(0.1, 0.9)]))  # only 1 point
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_NO_TRAJECTORY]


def test_release_direction_with_field_only_points_degrades() -> None:
    rule = {"type": "release_direction", "params": {"axis": "y", "sign": -1}}
    traj = [
        sc.TrajectoryPoint(None, None, field_x=10.0, field_y=0.0),
        sc.TrajectoryPoint(None, None, field_x=10.0, field_y=8.0),
    ]
    res = _score(_def("k", rule), _good_sub("k", traj, calibration_confidence=0.8))
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_NO_TRAJECTORY]


def test_zone_landmark_on_and_off() -> None:
    rule = {"type": "zone_landmark", "params": {"region": {"x": [0.0, 0.33], "y": [0.0, 0.35]}}}
    inside = _score(_def("k", rule), _good_sub("k", [sc.TrajectoryPoint(0.2, 0.2)]))
    assert inside.grade == sc.GRADE_ON
    outside = _score(_def("k", rule), _good_sub("k", [sc.TrajectoryPoint(0.95, 0.95)]))
    assert outside.grade == sc.GRADE_OFF


def test_depth_threshold_requires_calibration() -> None:
    rule = {"type": "depth_threshold", "params": {"min_depth_yards": 12.0}}
    # Frame coords only (no field_y) → cannot verify yards → needs_review.
    uncalibrated = [sc.TrajectoryPoint(0.5, 0.9 - 0.1 * i) for i in range(5)]
    res = _score(_def("k", rule), _good_sub("k", uncalibrated))
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_NO_CALIBRATED_TRAJECTORY]


def test_depth_threshold_graded_with_calibration() -> None:
    rule = {"type": "depth_threshold", "params": {"min_depth_yards": 12.0}}
    traj = [sc.TrajectoryPoint(0.5, 0.5, field_x=10.0, field_y=float(d)) for d in range(0, 16, 3)]
    res = _score(_def("k", rule), _good_sub("k", traj, calibration_confidence=0.8))
    assert res.grade == sc.GRADE_ON
    assert res.detail["depth_yards"] >= 12.0
    # confidence folds in calibration: 0.9 * 0.9 * 0.8
    assert abs(res.confidence - round(0.9 * 0.9 * 0.8, 3)) < 1e-6


def test_presence_rule() -> None:
    rule = {"type": "presence", "params": {"min_points": 3}}
    on = _score(_def("k", rule), _good_sub("k", [sc.TrajectoryPoint(0.5, 0.5)] * 5))
    assert on.grade == sc.GRADE_ON
    empty = _score(_def("k", rule), _good_sub("k", []))
    assert empty.grade == sc.GRADE_REVIEW
    assert empty.reason_codes == [sc.REASON_NO_TRAJECTORY]


def test_malformed_rule_params_degrade_to_review() -> None:
    rule = {"type": "zone_landmark", "params": {"region": {"x": [], "y": [0.0, 1.0]}}}
    res = _score(_def("k", rule), _good_sub("k", [sc.TrajectoryPoint(0.5, 0.5)]))
    assert res.grade == sc.GRADE_REVIEW
    assert res.reason_codes == [sc.REASON_UNKNOWN_RULE]


# ── Engine: confidence + experimental ───────────────────────────────────────


def test_confidence_and_uncertainty_complementary() -> None:
    rule = {"type": "presence", "params": {"min_points": 3}}
    res = _score(_def("k", rule), _good_sub("k", [sc.TrajectoryPoint(0.5, 0.5)] * 4))
    assert abs(res.confidence + res.uncertainty - 1.0) < 1e-6
    assert 0.0 <= res.confidence <= 1.0


def test_model_concept_is_experimental_with_reason() -> None:
    rule = {"type": "presence", "params": {"min_points": 3}}
    sub = _good_sub(
        "k", [sc.TrajectoryPoint(0.5, 0.5)] * 4, concept_source="model", concept_validated=False
    )
    res = _score(_def("k", rule), sub)
    assert res.experimental is True
    assert sc.REASON_UNVALIDATED_CONCEPT in res.reason_codes


def test_coach_validated_known_identity_not_experimental() -> None:
    rule = {"type": "presence", "params": {"min_points": 3}}
    res = _score(_def("k", rule), _good_sub("k", [sc.TrajectoryPoint(0.5, 0.5)] * 4))
    assert res.experimental is False


def test_probable_identity_flags_experimental() -> None:
    rule = {"type": "presence", "params": {"min_points": 3}}
    sub = _good_sub("k", [sc.TrajectoryPoint(0.5, 0.5)] * 4, identity_state="probable")
    res = _score(_def("k", rule), sub)
    assert res.experimental is True
    assert sc.REASON_PROBABLE_IDENTITY in res.reason_codes


# ── Engine: play-level aggregation ──────────────────────────────────────────


def _concept_from_seed(spec: dict[str, Any]) -> sc.ConceptDefinition:
    defs = [
        sc.AssignmentDefinition(
            key=a["key"],
            role=a["role"],
            intent=a.get("intent", ""),
            coaching_point=a.get("coaching_point", ""),
            rule=a["rule"],
            intended_route=a.get("intended_route"),
        )
        for a in spec["assignments"]
    ]
    return sc.ConceptDefinition(
        name=spec["name"],
        side=spec["side"],
        structure_kind=spec["structure_kind"],
        assignments=defs,
        coaching_points=spec.get("coaching_points", []),
    )


def test_score_play_aggregates_mixed_results() -> None:
    concept = sc.ConceptDefinition(
        name="t",
        side="defense",
        structure_kind="coverage",
        assignments=[
            _def(
                "zone",
                {"type": "zone_landmark", "params": {"region": {"x": [0.0, 0.5], "y": [0.0, 0.5]}}},
            ),
            _def("manual", {"type": "manual_only"}),
        ],
    )
    subs = {"zone": _good_sub("zone", [sc.TrajectoryPoint(0.2, 0.2)])}
    play = sc.score_play(concept, subs, identity_confidence_threshold=THRESHOLD)
    assert play.graded_count == 1
    assert play.on_count == 1
    assert play.needs_review_count == 1
    assert play.play_score == 1.0
    assert play.experimental is True  # any needs_review → experimental


def test_seed_concepts_degrade_to_review_without_substrate() -> None:
    for spec in SEED_CONCEPTS:
        concept = _concept_from_seed(spec)
        play = sc.score_play(concept, {}, identity_confidence_threshold=THRESHOLD)
        assert play.graded_count == 0
        assert play.play_score is None
        assert play.needs_review_count == len(concept.assignments)
        assert play.experimental is True


def test_seed_defense_zone_grades_with_substrate() -> None:
    concept = _concept_from_seed(COVER_3_SKY)
    # Drop the middle-third defender right into the middle deep third.
    subs = {"deep_third_middle": _good_sub("deep_third_middle", [sc.TrajectoryPoint(0.5, 0.15)])}
    play = sc.score_play(concept, subs, identity_confidence_threshold=THRESHOLD)
    assert play.on_count == 1
    graded = next(a for a in play.assignments if a.assignment_key == "deep_third_middle")
    assert graded.grade == sc.GRADE_ON


def test_seed_offense_depth_needs_calibration() -> None:
    concept = _concept_from_seed(FOUR_VERTICALS)
    # X vertical with frame-only trajectory → depth rule can't verify yards.
    subs = {
        "x_vertical": _good_sub(
            "x_vertical", [sc.TrajectoryPoint(0.1, 0.9 - 0.1 * i) for i in range(6)]
        )
    }
    play = sc.score_play(concept, subs, identity_confidence_threshold=THRESHOLD)
    x = next(a for a in play.assignments if a.assignment_key == "x_vertical")
    assert x.grade == sc.GRADE_REVIEW
    assert x.reason_codes == [sc.REASON_NO_CALIBRATED_TRAJECTORY]


def test_seed_specs_are_well_formed() -> None:
    known_rules = set(sc._RULES) | {"manual_only"}
    for spec in SEED_CONCEPTS:
        assert spec["side"] in {"offense", "defense"}
        keys = [a["key"] for a in spec["assignments"]]
        assert len(keys) == len(set(keys))
        for a in spec["assignments"]:
            assert a["rule"]["type"] in known_rules


# ── Router tests (mocked DB/auth) ───────────────────────────────────────────


def _user(role: UserRole) -> User:
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _mock_db(results: list[Any]):
    async def _db() -> AsyncGenerator[Any, None]:
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=results)
        session.flush = AsyncMock()
        yield session

    return _db


def _result(*, scalar: Any = None, scalars: list[Any] | None = None) -> MagicMock:
    r = MagicMock()
    r.scalar_one_or_none.return_value = scalar
    r.scalars.return_value.all.return_value = scalars or []
    return r


def test_concepts_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/playbook/concepts")
    assert resp.status_code == 401


def test_player_forbidden() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.player)
    app.dependency_overrides[get_db] = _mock_db([])
    with TestClient(app) as c:
        resp = c.get("/api/v1/playbook/concepts")
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_disabled_returns_404() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    fake = MagicMock()
    fake.playbook_enabled = False
    with patch("app.routers.playbook.get_settings", return_value=fake), TestClient(app) as c:
        resp = c.get("/api/v1/playbook/concepts")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_create_concept_rejects_bad_side() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([])
    body = {
        "name": "Bad Side",
        "side": "special",
        "structure_kind": "pass_concept",
        "assignments": [{"key": "a", "role": "X", "rule": {"type": "manual_only"}}],
    }
    with TestClient(app) as c:
        resp = c.post("/api/v1/playbook/concepts", json=body)
    app.dependency_overrides.clear()
    assert resp.status_code == 400


def test_create_concept_rejects_duplicate_keys() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([])
    body = {
        "name": "Dup Keys",
        "side": "offense",
        "structure_kind": "pass_concept",
        "assignments": [
            {"key": "a", "role": "X", "rule": {"type": "manual_only"}},
            {"key": "a", "role": "Z", "rule": {"type": "manual_only"}},
        ],
    }
    with TestClient(app) as c:
        resp = c.post("/api/v1/playbook/concepts", json=body)
    app.dependency_overrides.clear()
    assert resp.status_code == 400


def test_create_concept_rejects_soccer() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([])
    body = {
        "name": "High Press",
        "side": "defense",
        "structure_kind": "4-4-2",  # soccer formation
        "assignments": [{"key": "a", "role": "X", "rule": {"type": "manual_only"}}],
    }
    with TestClient(app) as c:
        resp = c.post("/api/v1/playbook/concepts", json=body)
    app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "soccer_resource_rejected"


def test_create_concept_rejects_soccer_in_assignment_text() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([])
    body = {
        "name": "Cover 3 Match",
        "side": "defense",
        "structure_kind": "coverage",
        "assignments": [
            {
                "key": "goalkeeper_help",
                "role": "Nickel",
                "coaching_point": "wall off the curl",
                "rule": {"type": "manual_only"},
            }
        ],
    }
    with TestClient(app) as c:
        resp = c.post("/api/v1/playbook/concepts", json=body)
    app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "soccer_resource_rejected"


def test_create_concept_rejects_invalid_rule_schema() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([])
    body = {
        "name": "Zone Match",
        "side": "defense",
        "structure_kind": "coverage",
        "assignments": [
            {
                "key": "curl_flat",
                "role": "Nickel",
                "rule": {
                    "type": "zone_landmark",
                    "params": {"region": {"x": [], "y": [0.0, 0.5]}},
                },
            }
        ],
    }
    with TestClient(app) as c:
        resp = c.post("/api/v1/playbook/concepts", json=body)
    app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert "zone_landmark region.x" in resp.json()["detail"]


def test_update_concept_rejects_soccer_in_assignment_text() -> None:
    concept = MagicMock()
    concept.id = uuid.uuid4()
    concept.name = "Cover 3 Sky"
    concept.structure_kind = "coverage"
    concept.description = "base coverage"
    concept.assignments = [{"key": "hook", "role": "Mike", "rule": {"type": "manual_only"}}]
    concept.coaching_points = ["carry verticals"]
    concept.is_active = True
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([_result(scalar=concept)])
    body = {
        "assignments": [
            {
                "key": "hook",
                "role": "goalkeeper",
                "coaching_point": "carry verticals",
                "rule": {"type": "manual_only"},
            }
        ]
    }
    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/playbook/concepts/{concept.id}", json=body)
    app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "soccer_resource_rejected"


def test_overlay_no_concept_linked() -> None:
    clip = MagicMock()
    clip.id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    # 1: select Clip -> clip; 2: select PlayConcept links -> []
    app.dependency_overrides[get_db] = _mock_db([_result(scalar=clip), _result(scalars=[])])
    with TestClient(app) as c:
        resp = c.get(f"/api/v1/playbook/clips/{clip.id}/overlay")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert data["overlays"] == []
    assert data["experimental"] is False
    assert "No playbook concept" in data["reason"]


def test_overlay_rolls_up_play_execution_from_coach_overrides() -> None:
    clip = MagicMock()
    clip.id = uuid.uuid4()
    concept = MagicMock()
    concept.id = uuid.uuid4()
    concept.name = "Cover 3 Sky"
    concept.side = "defense"
    concept.structure_kind = "coverage"
    concept.coaching_points = []
    link = MagicMock()
    link.source = "coach"
    link.validated = True
    definition = sc.AssignmentDefinition(
        key="hook", role="Mike", intent="", coaching_point="", rule={"type": "manual_only"}
    )
    auto_result = sc.AssignmentScoreResult(
        assignment_key="hook",
        grade=sc.GRADE_OFF,
        score=0.2,
        confidence=0.8,
        uncertainty=0.2,
        reason_codes=[sc.REASON_DIVERGED],
        experimental=False,
        detail={},
    )
    play = sc.PlayExecutionResult(
        play_score=0.2,
        graded_count=1,
        on_count=0,
        off_count=1,
        needs_review_count=0,
        confidence=0.8,
        experimental=False,
        assignments=[auto_result],
    )
    persisted = MagicMock()
    persisted.id = uuid.uuid4()
    persisted.status = "coach_overridden"
    persisted.coach_grade = sc.GRADE_ON
    persisted.coach_comment = "Corrected by coach"
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([_result(scalar=clip)])
    with (
        patch(
            "app.routers.playbook._links_with_concepts",
            new=AsyncMock(return_value=[(link, concept)]),
        ),
        patch("app.routers.playbook._score_link", new=AsyncMock(return_value=(play, [definition]))),
        patch(
            "app.routers.playbook._persisted_scores",
            new=AsyncMock(return_value={"hook": persisted}),
        ),
        TestClient(app) as c,
    ):
        resp = c.get(f"/api/v1/playbook/clips/{clip.id}/overlay")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()["overlays"][0]
    assert data["assignments"][0]["grade"] == sc.GRADE_ON
    assert data["play_execution"]["on_count"] == 1
    assert data["play_execution"]["off_count"] == 0
    assert data["play_execution"]["play_score"] == 1.0


def test_overlay_clip_not_found() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([_result(scalar=None)])
    with TestClient(app) as c:
        resp = c.get(f"/api/v1/playbook/clips/{uuid.uuid4()}/overlay")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


def test_score_clip_rolls_up_play_execution_from_coach_overrides() -> None:
    clip = MagicMock()
    clip.id = uuid.uuid4()
    concept = MagicMock()
    concept.id = uuid.uuid4()
    link = MagicMock()
    definition = sc.AssignmentDefinition(
        key="hook", role="Mike", intent="", coaching_point="", rule={"type": "manual_only"}
    )
    auto_result = sc.AssignmentScoreResult(
        assignment_key="hook",
        grade=sc.GRADE_OFF,
        score=0.2,
        confidence=0.8,
        uncertainty=0.2,
        reason_codes=[sc.REASON_DIVERGED],
        experimental=False,
        detail={},
    )
    play = sc.PlayExecutionResult(
        play_score=0.2,
        graded_count=1,
        on_count=0,
        off_count=1,
        needs_review_count=0,
        confidence=0.8,
        experimental=False,
        assignments=[auto_result],
    )
    persisted = MagicMock()
    persisted.id = uuid.uuid4()
    persisted.status = "coach_overridden"
    persisted.coach_grade = sc.GRADE_ON
    persisted.coach_comment = "Corrected by coach"
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([_result(scalar=clip)])
    with (
        patch(
            "app.routers.playbook._links_with_concepts",
            new=AsyncMock(return_value=[(link, concept)]),
        ),
        patch("app.routers.playbook._score_link", new=AsyncMock(return_value=(play, [definition]))),
        patch(
            "app.routers.playbook._persist_scores",
            new=AsyncMock(return_value={"hook": persisted}),
        ),
        TestClient(app) as c,
    ):
        resp = c.post(f"/api/v1/playbook/clips/{clip.id}/score")
    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()[0]
    assert data["assignments"][0]["grade"] == sc.GRADE_ON
    assert data["play_execution"]["on_count"] == 1
    assert data["play_execution"]["off_count"] == 0
    assert data["play_execution"]["play_score"] == 1.0


def test_override_invalid_grade() -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.coach)
    app.dependency_overrides[get_db] = _mock_db([])
    with TestClient(app) as c:
        resp = c.patch(f"/api/v1/playbook/scores/{uuid.uuid4()}", json={"coach_grade": "bogus"})
    app.dependency_overrides.clear()
    assert resp.status_code == 400
