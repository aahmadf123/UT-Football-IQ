"""Explicit protocol observations test mechanics, never football accuracy."""

import pytest

from pipeline.observed_skill_outcomes import observed_skill_outcomes


def test_result_contradiction_also_blocks_reception_based_cut():
    tracks, events = fixture()
    events[1]["attributes"]["outcome"] = "incomplete"
    names = {
        r["metric_name"]
        for r in observed_skill_outcomes(tracks, events, analytics_safe=True, attacking_direction=1)
    }
    assert "observed_cut_location" not in names


@pytest.mark.parametrize("fault", ["not_observed", "defensive_possession", "different_receiver"])
def test_pass_grade_abstains_when_reviewed_reception_contradicts_completion(fault):
    tracks, events = fixture()
    if fault == "not_observed":
        events[2]["attributes"]["completion_observed"] = False
    elif fault == "defensive_possession":
        events[2]["attributes"]["possession_side"] = "defense"
    else:
        events[2]["attributes"]["actor_id"] = "defender"
    names = {
        r["metric_name"]
        for r in observed_skill_outcomes(tracks, events, analytics_safe=True, attacking_direction=1)
    }
    assert "observed_target" in names
    assert not names.intersection(
        {"target_catch_outcome", "contested_target_outcome", "coverage_target_outcome"}
    )


def test_conflicting_event_id_never_counts_a_tackle_twice():
    import copy

    tracks, events = fixture()
    repeated = copy.deepcopy(events[-1])
    repeated["timestamp_seconds"] = 0.9
    rows = observed_skill_outcomes(
        tracks, [*events, repeated], analytics_safe=True, attacking_direction=1
    )
    assert "observed_missed_tackle" not in {r["metric_name"] for r in rows}


@pytest.mark.parametrize(
    "fault",
    [
        "side_missing",
        "side_conflict",
        "cross_shot_result",
        "contradictory_result",
        "conflicting_result",
        "cut_after_end",
        "cut_start_other_shot",
    ],
)
def test_conflicting_or_expired_evidence_abstains(fault):
    import copy

    tracks, events = fixture()
    if fault == "side_missing":
        tracks[0].pop("side_of_ball")
    elif fault == "side_conflict":
        tracks[0]["team_label"] = "defense"
    elif fault == "cross_shot_result":
        events[1]["shot_id"] = "different"
    elif fault == "contradictory_result":
        events[1]["attributes"]["outcome"] = "interception"
    elif fault == "conflicting_result":
        duplicate = copy.deepcopy(events[1])
        duplicate["id"] = "contradiction"
        duplicate["attributes"]["outcome"] = "incomplete"
        events.append(duplicate)
    elif fault == "cut_after_end":
        events[4]["timestamp_seconds"] = 1.1
        tracks[0]["track_points"].append(
            {**tracks[0]["track_points"][-1], "timestamp_seconds": 1.1, "frame_number": 11}
        )
    else:
        events[2]["shot_id"] = "another-shot"
    names = {
        r["metric_name"]
        for r in observed_skill_outcomes(tracks, events, analytics_safe=True, attacking_direction=1)
    }
    if fault in {"cut_after_end", "cut_start_other_shot"}:
        assert "observed_cut_location" not in names
    else:
        assert "observed_yac" not in names


def fixture():
    tracks = [
        {
            "id": identity,
            "source": "human",
            "side_of_ball": side,
            "track_points": [
                {
                    "timestamp_seconds": i / 10,
                    "frame_number": i,
                    "field_x": 50 - i / 10,
                    "field_y": 0.0,
                    "shot_id": "shot",
                    "visibility_state": "observed",
                }
                for i in range(11)
            ],
        }
        for identity, side in (("receiver", "offense"), ("defender", "defense"))
    ]

    def event(kind, time, **attrs):
        return {
            "id": f"{kind}-{time}",
            "event_type": kind,
            "timestamp_seconds": time,
            "source": "human",
            "shot_id": "shot",
            "attributes": {"possession_id": "pass-one", **attrs},
        }

    events = [
        event(
            "pass_target",
            0.1,
            actor_id="receiver",
            opponent_id="defender",
            responsibility_confirmed=True,
            forward_pass=True,
        ),
        event("pass_result", 0.4, actor_id="receiver", outcome="complete", contested=True),
        event(
            "reception",
            0.4,
            actor_id="receiver",
            forward_pass=True,
            completion_observed=True,
            possession_side="offense",
        ),
        event(
            "possession_end",
            1.0,
            actor_id="receiver",
            possession_ended=True,
            terminal_reason="out_of_bounds",
        ),
        event("carrier_cut", 0.6, actor_id="receiver", cut_observed=True),
        event(
            "tackle_outcome",
            0.8,
            actor_id="defender",
            opponent_id="receiver",
            outcome="missed",
            tackle_observed=True,
            forced_by_carrier=True,
        ),
    ]
    return tracks, events


def test_explicit_pass_responsibility_contest_yac_cut_and_tackle():
    tracks, events = fixture()
    rows = observed_skill_outcomes(tracks, events, analytics_safe=True, attacking_direction=1)
    by_name = {row["metric_name"]: row for row in rows}
    assert by_name["observed_yac"]["metric_value"]["yards"] == pytest.approx(-0.6)
    assert by_name["target_catch_outcome"]["metric_value"]["completed"] == 1
    assert by_name["coverage_target_outcome"]["tracklet_id"] == "defender"
    assert by_name["contested_target_outcome"]["metric_value"]["completed"] == 1
    assert by_name["observed_cut_location"]["metric_value"]["field_x"] == 49.4
    assert by_name["observed_missed_tackle"]["metric_value"]["count"] == 1
    assert by_name["observed_missed_tackle"]["tracklet_id"] == "defender"
    assert by_name["observed_forced_missed_tackle"]["tracklet_id"] == "receiver"
    assert all(row["experimental_flag"] for row in rows)


def test_motion_alone_or_missing_results_never_invents_outcomes():
    tracks, events = fixture()
    assert observed_skill_outcomes(tracks, [], analytics_safe=True, attacking_direction=1) == []
    target = events[:1]
    names = {r["metric_name"] for r in observed_skill_outcomes(tracks, target)}
    assert names == {"observed_target"}
    assert "observed_yac" not in {
        r["metric_name"]
        for r in observed_skill_outcomes(
            tracks, events, analytics_safe=False, attacking_direction=1
        )
    }


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_actor",
        "shot_cut",
        "unknown_visibility",
        "unknown_shot",
        "future_end",
        "synthetic",
        "interception",
    ],
)
def test_incomplete_or_contradictory_possession_cannot_measure_yac(fault):
    tracks, events = fixture()
    if fault == "wrong_actor":
        events[3]["attributes"]["actor_id"] = "defender"
    elif fault == "shot_cut":
        tracks[0]["track_points"][6]["shot_id"] = "another"
    elif fault == "unknown_visibility":
        tracks[0]["track_points"][6].pop("visibility_state")
    elif fault == "unknown_shot":
        events[2]["shot_id"] = "unknown"
    elif fault == "future_end":
        events[3]["timestamp_seconds"] = 0.3
    elif fault == "synthetic":
        tracks[0]["track_points"][6]["source"] = "synthetic_test"
    else:
        events[2]["attributes"]["possession_side"] = "defense"
    rows = observed_skill_outcomes(tracks, events, analytics_safe=True, attacking_direction=1)
    assert "observed_yac" not in {row["metric_name"] for row in rows}


def test_responsibility_and_tackle_outcomes_require_explicit_evidence():
    tracks, events = fixture()
    del events[0]["attributes"]["responsibility_confirmed"]
    events[-1]["attributes"]["tackle_observed"] = False
    rows = observed_skill_outcomes(tracks, events, analytics_safe=True, attacking_direction=-1)
    assert "coverage_target_outcome" not in {row["metric_name"] for row in rows}
    assert "observed_missed_tackle" not in {row["metric_name"] for row in rows}
    assert next(r for r in rows if r["metric_name"] == "observed_yac")["metric_value"][
        "yards"
    ] == pytest.approx(0.6)
