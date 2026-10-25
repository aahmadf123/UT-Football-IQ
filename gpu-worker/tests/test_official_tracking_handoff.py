"""Officials remain visible but never enter the player-analysis bundle."""

from unittest.mock import Mock

import pytest

from pipeline import stage_track
from pipeline.detection.official_suppressor import filter_non_players
from pipeline.tracker_models import IoUTracker


def detection(kind, frame, shot="one"):
    return {"class": kind, "bbox": [10, 10, 30, 50], "confidence": .9,
            "timestamp_seconds": frame / 30, "shot_id": shot, "source": "real"}


@pytest.mark.parametrize("variant", ["iou-tracker", "botsort", "strongsort"])
def test_official_pool_cannot_join_player_or_enter_player_metrics(monkeypatch, variant):
    write = Mock(side_effect=lambda *a, **kw: {"id": f"row-{write.call_count}"})
    monkeypatch.setattr(stage_track.backend, "create_tracklet", write)
    result = stage_track.run(
        "clip", {"0": [detection("player", 0), detection("official", 0)],
                 "1": [detection("player", 1), detection("official", 1)]},
        30, "job", variant=variant, detector_provenance={"source": "real"},
    )
    assert len(result["tracklets"]) == len(result["non_player_tracklets"]) == 1
    assert result["tracklets"][0]["class"] == "player"
    official = result["non_player_tracklets"][0]
    assert official["team_label"] == "official"
    assert official["provenance"]["actor_kind"] == "official"
    assert official["provenance"]["actor_classification"]["validated"] is False
    assert all(p.get("field_x") is None for p in official["track_points"])
    assert filter_non_players(result["tracklets"] + result["non_player_tracklets"]) == result["tracklets"]


def test_one_observed_referee_frame_is_preserved_and_shots_never_join(monkeypatch):
    write = Mock(side_effect=lambda *a, **kw: {"id": f"row-{write.call_count}"})
    monkeypatch.setattr(stage_track.backend, "create_tracklet", write)
    result = stage_track.run(
        "clip", {"0": [detection("official", 0)],
                 "1": [detection("official", 1, "two")]}, 30, "job", tracker=IoUTracker(),
    )
    assert result["tracklets"] == []
    assert [len(t["track_points"]) for t in result["non_player_tracklets"]] == [1, 1]


def test_provenance_official_filter_handles_backend_shape():
    player = {"provenance": {"actor_kind": "player"}}
    assert filter_non_players([player, {"provenance": {"actor_kind": "official"}}]) == [player]
    legacy_player = {"team_label": "unknown", "role": "unknown"}
    assert filter_non_players([
        legacy_player, {"provenance": {"actor_kind": "unknown"}},
        {"class": "player", "provenance": {"actor_kind": "official"}},
    ]) == [legacy_player]
