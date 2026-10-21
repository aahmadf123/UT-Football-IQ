"""Which colour is the offence: formation first, then possession + jerseys."""

from __future__ import annotations

import numpy as np

from pipeline.stage_labels import TEAM_CLASSIFICATION_MAX_FRAMES, _wanted_team_frames
from pipeline.team_classification.sides import (
    SideContext,
    formation_offense_cluster,
    jersey_offense_cluster,
    resolve_offense_cluster,
)

LIGHT, DARK = 0, 1
CENTROIDS = np.array([[200.0, 128.0, 128.0], [70.0, 110.0, 140.0], [120.0, 128.0, 128.0]])


def _formation(offense: int, defense: int, frames: range) -> tuple[list[dict], dict[str, int]]:
    """Offence attacks toward +x from a line at x=50; the defence keeps safeties deep."""
    spots = {
        offense: [(49.0, 20.0), (49.0, 23.0), (49.0, 26.0), (49.0, 29.0), (49.0, 32.0), (44.0, 26.0), (43.0, 30.0)],
        defense: [(51.0, 21.0), (51.0, 25.0), (51.0, 29.0), (51.0, 33.0), (57.0, 22.0), (62.0, 18.0), (63.0, 36.0)],
    }
    tracks, clusters = [], {}
    for cluster, places in spots.items():
        for i, (x, y) in enumerate(places):
            track_id = f"{cluster}-{i}"
            clusters[track_id] = cluster
            tracks.append(
                {"id": track_id, "track_points": [{"frame_number": f, "field_x": x, "field_y": y} for f in frames]}
            )
    return tracks, clusters


def test_formation_finds_the_offence_whatever_its_colour() -> None:
    for offense, defense in ((LIGHT, DARK), (DARK, LIGHT)):
        tracks, clusters = _formation(offense, defense, range(5))
        decision = formation_offense_cluster(tracks, clusters, (LIGHT, DARK), list(range(5)))
        assert decision.offense_cluster == offense
        assert decision.evidence == "formation_depth" and decision.frames_resolved == 5


def test_formation_needs_field_coordinates() -> None:
    tracks, clusters = _formation(LIGHT, DARK, range(5))
    for track in tracks:
        for point in track["track_points"]:
            point["field_x"] = point["field_y"] = None
    decision = formation_offense_cluster(tracks, clusters, (LIGHT, DARK), list(range(5)))
    assert decision.offense_cluster is None and decision.evidence == "formation_unresolved"


def test_possession_and_jerseys_follow_the_ball() -> None:
    road = SideContext(toledo_on="offense", toledo_light=True)  # Toledo in white, with the ball
    assert jersey_offense_cluster(CENTROIDS, (LIGHT, DARK), road).offense_cluster == LIGHT
    road_defending = SideContext(toledo_on="defense", toledo_light=True)
    assert jersey_offense_cluster(CENTROIDS, (LIGHT, DARK), road_defending).offense_cluster == DARK
    home = SideContext(toledo_on="offense", toledo_light=False)
    assert jersey_offense_cluster(CENTROIDS, (LIGHT, DARK), home).offense_cluster == DARK


def test_no_call_without_possession_or_on_alike_kits() -> None:
    special_teams = SideContext(toledo_on=None, toledo_light=True)
    assert jersey_offense_cluster(CENTROIDS, (LIGHT, DARK), special_teams).offense_cluster is None
    alike = np.array([[80.0, 128.0, 128.0], [70.0, 110.0, 140.0], [120.0, 128.0, 128.0]])
    decision = jersey_offense_cluster(alike, (LIGHT, DARK), SideContext("offense", True))
    assert decision.offense_cluster is None and decision.evidence == "jerseys_alike"


def test_formation_wins_over_the_jersey_convention() -> None:
    # A colour-rush game: Toledo at home in white. The formation is the truth.
    tracks, clusters = _formation(LIGHT, DARK, range(4))
    wrong_convention = SideContext(toledo_on="defense", toledo_light=True)
    decision = resolve_offense_cluster(tracks, clusters, CENTROIDS, (LIGHT, DARK), list(range(4)), wrong_convention)
    assert decision.offense_cluster == LIGHT and decision.evidence == "formation_depth"


def test_frames_cover_tracks_that_start_after_the_snap() -> None:
    def track(track_id: str, frames: range) -> dict:
        return {"id": track_id, "track_points": [{"frame_number": f, "bbox": [0, 0, 1, 1]} for f in frames]}

    tracks = [track("starter", range(0, 400)), track("late", range(300, 330)), track("blink", range(390, 392))]
    frames = _wanted_team_frames(tracks, snap_frame=0)
    assert frames[0] == 0
    assert any(300 <= f < 330 for f in frames)  # the old window (frames 0-29) missed it
    assert any(390 <= f < 392 for f in frames)
    assert len(frames) <= TEAM_CLASSIFICATION_MAX_FRAMES


def test_frame_cap_keeps_one_look_per_track() -> None:
    tracks = [
        {"id": f"t{i}", "track_points": [{"frame_number": 10 * i + j, "bbox": [0, 0, 1, 1]} for j in range(3)]}
        for i in range(TEAM_CLASSIFICATION_MAX_FRAMES + 20)
    ]
    frames = _wanted_team_frames(tracks, snap_frame=0)
    assert len(frames) == TEAM_CLASSIFICATION_MAX_FRAMES


def test_side_context_reads_possession_and_home_team() -> None:
    from unittest.mock import patch

    from pipeline.orchestrator import _side_context

    away_game = {"our_possession": "defense", "label_data": {"cfbd": {"home_team": "Michigan State"}}}
    with patch("pipeline.backend.fetch_clip", return_value=away_game):
        assert _side_context("c") == SideContext(toledo_on="defense", toledo_light=True)
    home_kick = {"our_possession": "special_teams", "label_data": {"cfbd": {"home_team": "Toledo"}}}
    with patch("pipeline.backend.fetch_clip", return_value=home_kick):
        assert _side_context("c") == SideContext(toledo_on=None, toledo_light=False)
    with patch("pipeline.backend.fetch_clip", side_effect=RuntimeError("offline")):
        assert _side_context("c") == SideContext()
