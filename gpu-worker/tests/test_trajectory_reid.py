"""Tests for trajectory-prior re-ID (Issue #131).

Covers the pure-NumPy Hungarian solver and the constant-velocity + roster +
team-prior assignment that recovers identity when OCR fails.
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.tracking.trajectory_prior_reid import (
    KnownPlayer,
    TrajectoryPriorReID,
    UnknownTrack,
    hungarian,
)

# ── Hungarian solver ──────────────────────────────────────────────────────────


def test_hungarian_empty() -> None:
    assert hungarian(np.zeros((0, 0))) == []


def test_hungarian_identity_optimal() -> None:
    cost = np.array([[1.0, 9.0], [9.0, 1.0]])
    assert sorted(hungarian(cost)) == [(0, 0), (1, 1)]


def test_hungarian_picks_min_cost_permutation() -> None:
    cost = np.array([[9.0, 1.0], [1.0, 9.0]])
    assert sorted(hungarian(cost)) == [(0, 1), (1, 0)]


def test_hungarian_rectangular_more_cols() -> None:
    cost = np.array([[1.0, 5.0, 9.0]])
    pairs = hungarian(cost)
    assert pairs == [(0, 0)]


def test_hungarian_rectangular_more_rows_no_duplicate_cols() -> None:
    # 3 rows, 2 cols: exactly 2 pairs, each column used at most once, min cost.
    cost = np.array([[1.0, 9.0], [9.0, 1.0], [2.0, 2.0]])
    pairs = hungarian(cost)
    cols = [c for _, c in pairs]
    assert len(pairs) == 2
    assert len(set(cols)) == len(cols)
    assert sorted(pairs) == [(0, 0), (1, 1)]


def test_hungarian_matches_brute_force_optimum() -> None:
    import itertools

    rng = np.random.default_rng(0)
    m = rng.random((4, 4))
    best = min(
        sum(m[i, perm[i]] for i in range(4))
        for perm in itertools.permutations(range(4))
    )
    got = sum(m[r, c] for r, c in hungarian(m))
    assert abs(best - got) < 1e-9


# ── Trajectory-prior assignment ───────────────────────────────────────────────


def test_assign_matches_nearest_predicted_player() -> None:
    # Player 7 last seen at (100, 100) moving +10/frame; at frame 5 it should be
    # near (150, 100). An OCR-missing track sits there.
    players = [
        KnownPlayer("p7", position=(100, 100), velocity=(10, 0), last_frame=0),
        KnownPlayer("p9", position=(500, 400), velocity=(0, 0), last_frame=0),
    ]
    tracks = [UnknownTrack(index=3, position=(150, 100), frame=5)]
    out = TrajectoryPriorReID().assign(tracks, players)
    assert out == {3: "p7"}


def test_team_constraint_blocks_cross_team_match() -> None:
    # The only spatially-close player is on the other team -> no assignment.
    players = [KnownPlayer("d1", position=(150, 100), team="defense", last_frame=0)]
    tracks = [UnknownTrack(index=1, position=(150, 100), team="offense", frame=0)]
    assert TrajectoryPriorReID().assign(tracks, players) == {}


def test_far_track_left_unassigned_by_gate() -> None:
    players = [KnownPlayer("p1", position=(0, 0), last_frame=0)]
    tracks = [UnknownTrack(index=1, position=(5000, 5000), frame=0)]
    assert TrajectoryPriorReID().assign(tracks, players) == {}


def test_no_players_or_no_tracks_returns_empty() -> None:
    tpr = TrajectoryPriorReID()
    assert tpr.assign([], [KnownPlayer("p", (0, 0))]) == {}
    assert tpr.assign([UnknownTrack(0, (0, 0))], []) == {}


def test_two_tracks_two_players_one_to_one() -> None:
    players = [
        KnownPlayer("a", position=(100, 100), last_frame=0),
        KnownPlayer("b", position=(200, 100), last_frame=0),
    ]
    tracks = [
        UnknownTrack(index=0, position=(205, 100), frame=0),
        UnknownTrack(index=1, position=(102, 100), frame=0),
    ]
    assert TrajectoryPriorReID().assign(tracks, players) == {0: "b", 1: "a"}
