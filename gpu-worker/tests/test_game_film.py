"""Structure recovery for edited coaches' game film (pure logic, no video)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.game_film import (
    GameFilmStructure,
    Shot,
    ViewSegment,
    cuts_from_signal,
    group_plays,
    looks_like_edited_film,
    parse_scoreboard,
    play_clip_specs,
    split_views,
    view_label,
)


def test_cuts_ignore_gradual_change_and_find_jumps() -> None:
    signal = [0.0] + [0.05] * 50 + [1.4] + [0.05] * 50 + [1.2] + [0.06] * 30
    assert cuts_from_signal(signal) == [0, 51, 102]


def _card(t: float) -> Shot:
    return Shot(t, t + 3.0, 0.08)


def test_edited_film_needs_regular_cards() -> None:
    shots: list[Shot] = []
    t = 0.0
    for _ in range(12):
        shots.append(_card(t))
        shots.append(Shot(t + 3, t + 28, 0.85))
        t += 28
    assert looks_like_edited_film(shots)
    # Raw practice clip: all field, no cards.
    assert not looks_like_edited_film([Shot(0, 13, 0.9)])


def test_view_label_thresholds() -> None:
    assert view_label(16)[0] == "sideline"
    assert view_label(2)[0] == "endzone"
    assert view_label(8)[0] == "unknown"


def test_split_views_handles_dissolve_inside_one_shot() -> None:
    shot = Shot(10.0, 40.0, 0.85)
    # Sideline for 15 s, then end zone (no hard cut), sampled at 2 Hz.
    samples = [(10.0 + i / 2, "sideline" if i < 30 else "endzone", 0.8) for i in range(60)]
    # One misread sample in the middle of the sideline part is smoothed away.
    samples[10] = (15.0, "endzone", 0.6)
    segments = split_views([shot], samples)
    assert [s.view for s in segments] == ["sideline", "endzone"]
    assert segments[0].start == 10.0 and segments[-1].end == 40.0
    assert abs(segments[0].end - 25.0) < 0.6


def test_cards_stay_cards_and_group_plays() -> None:
    segments = [
        ViewSegment(0, 3, "card", 0.9),
        ViewSegment(3, 16, "sideline", 0.8),
        ViewSegment(16, 28, "endzone", 0.9),
        ViewSegment(28, 31, "card", 0.9),
        ViewSegment(31, 31.5, "sideline", 0.5),  # transition sliver: dropped
        ViewSegment(31.5, 45, "sideline", 0.8),
    ]
    plays = group_plays(segments)
    assert [p.number for p in plays] == [1, 2]
    assert [v.view for v in plays[0].views] == ["sideline", "endzone"]
    assert plays[0].card_time == 1.5 and plays[1].card_time == 29.5
    assert [v.view for v in plays[1].views] == ["sideline"]


def test_clip_specs_pair_angles_by_play() -> None:
    plays = group_plays(
        [
            ViewSegment(0, 3, "card", 0.9),
            ViewSegment(3, 16, "sideline", 0.8),
            ViewSegment(16, 28, "endzone", 0.9),
        ]
    )
    plays[0].scoreboard = {"down": 1, "distance": 10}
    specs = play_clip_specs(GameFilmStructure(edited=True, plays=plays))
    assert [(s["play_number"], s["view"]) for s in specs] == [(1, "sideline"), (1, "endzone")]
    assert specs[1]["scoreboard"] == {"down": 1, "distance": 10}


def test_parse_scoreboard_from_ocr_text() -> None:
    # Actual EasyOCR output on a Spartan Stadium card.
    texts = ["IAVIUM", "@uGog 0", "0", "1ST QUARTER", "1ST & 10", "15.00", "BALL ON 25}"]
    parsed = parse_scoreboard(texts)
    assert parsed is not None
    assert parsed["quarter"] == 1
    assert (parsed["down"], parsed["distance"], parsed["goal_to_go"]) == (1, 10, False)
    assert parsed["clock"] == "15:00" and parsed["clock_seconds"] == 900
    assert parsed["ball_on"] == 25


def test_parse_scoreboard_variants() -> None:
    parsed = parse_scoreboard(["3RD & GOAL", "Q4", "2:O7"])
    assert parsed is not None
    assert parsed["down"] == 3 and parsed["goal_to_go"] and parsed["distance"] is None
    assert parsed["quarter"] == 4
    assert parsed["clock_seconds"] == 127
    assert parse_scoreboard(["SPARTANS", "WELCOME"]) is None


def test_segmenter_falls_back_when_film_is_not_edited(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path

    from pipeline.segment_models import GameFilmSegmenter, SegmenterBase

    class Fallback(SegmenterBase):
        source = "optical_flow"

        def segment(self, video_path: Path) -> list[dict]:  # type: ignore[type-arg]
            return [{"time_s": 4.0, "confidence": 0.7, "source": "optical_flow"}]

    segmenter = GameFilmSegmenter(fallback=Fallback())
    # Not a video at all: analysis abstains and the fallback answers.
    missing = tmp_path / "not-a-video.mp4"
    missing.write_bytes(b"")
    assert segmenter.segment(missing) == [
        {"time_s": 4.0, "confidence": 0.7, "source": "optical_flow"}
    ]
    assert segmenter.clip_specs == []
    assert segmenter.source == "optical_flow"


def test_parse_scoreboard_tolerates_stylized_font_misreads() -> None:
    # Real EasyOCR output from the 2026 Michigan State game film.
    cases = [
        (["1ST QUARTER", "PND & 5", "1443", "BALL ON 30"], (2, 5, "14:43", 30, 1)),
        (["1ST QUARTER", "ATH & 2", "12.13", "BALL ON 45"], (4, 2, "12:13", 45, 1)),
        (["1ST QUARTER", "BRD & 7", "202", "BALL ON 25"], (3, 7, "2:02", 25, 1)),
        (["1ST QUARTER", "3R1 & 7", "2.41", "BALL ON 25"], (3, 7, "2:41", 25, 1)),
        (["ZND QUARTER", "END & 2", "852", "BALL ON 12"], (2, 2, "8:52", 12, 2)),
    ]
    for texts, (down, distance, clock, ball_on, quarter) in cases:
        parsed = parse_scoreboard(texts)
        assert parsed is not None, texts
        assert (parsed["down"], parsed["distance"]) == (down, distance), texts
        assert parsed["clock"] == clock, texts
        assert parsed["ball_on"] == ball_on and parsed["quarter"] == quarter, texts


def test_blank_card_has_no_down() -> None:
    # Kickoffs and extra points show "&" with nothing around it.
    parsed = parse_scoreboard(["1ST QUARTER", "&", "15.00", "BALL ON"])
    assert parsed is not None and "down" not in parsed
    assert parsed["quarter"] == 1 and parsed["clock"] == "15:00"


def test_undecided_angles_follow_film_order() -> None:
    plays = group_plays(
        [
            ViewSegment(0, 3, "card", 0.9),
            ViewSegment(3, 16, "unknown", 0.3),
            ViewSegment(16, 28, "unknown", 0.3),
            ViewSegment(28, 31, "card", 0.9),
            ViewSegment(31, 44, "sideline", 0.8),
            ViewSegment(44, 57, "unknown", 0.3),
        ]
    )
    assert [v.view for v in plays[0].views] == ["sideline", "endzone"]
    assert [v.view for v in plays[1].views] == ["sideline", "endzone"]
    assert all(v.confidence <= 0.4 for v in plays[0].views)
