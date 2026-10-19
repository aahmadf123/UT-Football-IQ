from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.routers.game_summary import (
    _play_clips,
    is_explosive,
    is_success,
    play_kind,
    summarize_side,
)


def _play(play_type: str, down: int | None, distance: int | None, gained: int, **extra):
    return {
        "play_type": play_type,
        "down": down,
        "distance": distance,
        "yards_gained": gained,
        "offense": "Toledo",
        "defense": "Michigan State",
        **extra,
    }


def test_play_kind_reads_cfbd_types_and_fumble_text():
    assert play_kind({"play_type": "Rushing Touchdown"}) == "rush"
    assert play_kind({"play_type": "Sack"}) == "pass"
    assert play_kind({"play_type": "Punt"}) is None
    assert (
        play_kind({"play_type": "Fumble Recovery (Opponent)", "play_text": "J. Doe run for 3"})
        == "rush"
    )
    assert (
        play_kind({"play_type": "Fumble Recovery (Own)", "play_text": "Smith pass complete"})
        == "pass"
    )


def test_success_follows_the_down_thresholds():
    assert is_success(_play("Rush", 1, 10, 5)) is True
    assert is_success(_play("Rush", 1, 10, 4)) is False
    assert is_success(_play("Rush", 2, 10, 7)) is True
    assert is_success(_play("Rush", 2, 10, 6)) is False
    assert is_success(_play("Rush", 3, 4, 4)) is True
    assert is_success(_play("Rush", 3, 4, 3)) is False
    assert is_success(_play("Kickoff", None, None, 30)) is None


def test_explosive_thresholds_differ_for_runs_and_passes():
    assert is_explosive(_play("Rush", 1, 10, 10), "rush")
    assert not is_explosive(_play("Pass Reception", 1, 10, 19), "pass")
    assert is_explosive(_play("Pass Reception", 1, 10, 20), "pass")


def test_side_summary_counts_and_keeps_the_clips_behind_each_number():
    ids = [uuid.uuid4() for _ in range(6)]
    plays = [
        (_play("Rush", 1, 10, 12, ppa=1.2), ids[0]),  # success, explosive
        (_play("Pass Incompletion", 2, 10, 0, ppa=-0.6), ids[1]),
        (_play("Sack", 3, 10, -7, ppa=-1.5), ids[2]),  # negative, failed 3rd
        (_play("Pass Reception", 3, 6, 8, ppa=1.0), ids[3]),  # 3rd-down conversion
        (_play("Pass Interception Return", 1, 10, 0, ppa=-3.0), ids[4]),  # turnover
        (_play("Punt", 4, 9, 40), ids[5]),  # special teams: excluded
    ]
    side = summarize_side(plays)
    assert side.plays == 5
    assert side.rush_plays == 1 and side.pass_plays == 4
    assert side.yards == 13
    assert side.third_down_attempts == 2 and side.third_down_conversions == 1
    assert side.explosive_plays == 1
    assert side.turnovers == 1
    assert side.success_rate == round(2 / 5, 3)
    assert side.epa_per_play == round((1.2 - 0.6 - 1.5 + 1.0 - 3.0) / 5, 3)
    groups = {g.key: g.clip_ids for g in side.groups}
    assert groups["third_down_converted"] == [ids[3]]
    assert groups["negative"] == [ids[2]]
    assert ids[5] not in groups["all"]
    assert "scoring" not in groups  # empty groups are omitted
    third = next(d for d in side.by_down if d.down == 3)
    assert third.plays == 2 and third.success_rate == 0.5
    assert third.clip_ids == [ids[2], ids[3]]


def test_unclassified_scrimmage_fumble_stays_in_totals_but_not_run_or_pass():
    fumble_id, punt_id = uuid.uuid4(), uuid.uuid4()
    side = summarize_side(
        [
            (
                _play("Fumble Recovery (Opponent)", 2, 7, 0, play_text=None),
                fumble_id,
            ),
            (_play("Punt", 4, 9, 40), punt_id),
        ]
    )

    assert side.plays == 1 and side.turnovers == 1
    assert side.rush_plays == 0 and side.pass_plays == 0
    groups = {group.key: group.clip_ids for group in side.groups}
    assert groups["all"] == [fumble_id]
    assert groups["turnovers"] == [fumble_id]
    assert punt_id not in groups["all"]
    assert side.by_down[0].clip_ids == [fumble_id]


def test_one_clip_per_play_prefers_the_sideline_angle():
    end_zone, sideline, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cfbd = _play("Rush", 1, 10, 3)
    clips = [
        SimpleNamespace(
            id=end_zone,
            play_number=4,
            start_time=10.0,
            label_data={"view": "endzone", "cfbd": cfbd},
        ),
        SimpleNamespace(
            id=sideline,
            play_number=4,
            start_time=12.0,
            label_data={"view": "sideline", "cfbd": cfbd},
        ),
        SimpleNamespace(id=other, play_number=5, start_time=20.0, label_data={"view": "sideline"}),
    ]
    assert _play_clips(clips) == {4: (cfbd, sideline)}
