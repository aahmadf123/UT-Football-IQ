"""Tests for min-cost-flow track stitching (Issue #131, nightly only).

Verifies the pure-Python successive-shortest-path solver stitches fragmented
tracklets of the same identity within a single clip, respects the temporal gap
gate, and keeps genuinely-distinct tracklets separate. Single-camera only.
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.tracking.min_cost_flow_stitcher import (
    MinCostFlowStitcher,
    TrackletNode,
)


def _node(
    tid: str,
    start: int,
    end: int,
    start_pos: tuple[float, float],
    end_pos: tuple[float, float],
    vel: tuple[float, float] = (0.0, 0.0),
    emb: list[float] | None = None,
) -> TrackletNode:
    return TrackletNode(
        tracklet_id=tid,
        start_frame=start,
        end_frame=end,
        start_position=start_pos,
        end_position=end_pos,
        end_velocity=vel,
        embedding=np.asarray(emb, dtype=np.float64) if emb is not None else None,
    )


def test_empty_returns_no_groups() -> None:
    assert MinCostFlowStitcher().stitch([]) == MinCostFlowStitcher().stitch([])
    assert MinCostFlowStitcher().stitch([]).groups == []


def test_single_tracklet_is_its_own_identity() -> None:
    nodes = [_node("a", 0, 10, (0, 0), (10, 0))]
    result = MinCostFlowStitcher().stitch(nodes)
    assert result.groups == [["a"]]


def test_two_consecutive_fragments_stitched() -> None:
    # "a" ends at (50,0) moving +5/frame; "b" begins right where "a" would be.
    nodes = [
        _node("a", 0, 10, (0, 0), (50, 0), vel=(5, 0)),
        _node("b", 12, 20, (60, 0), (100, 0), vel=(5, 0)),
    ]
    result = MinCostFlowStitcher().stitch(nodes)
    assert len(result.groups) == 1
    assert sorted(result.groups[0]) == ["a", "b"]
    assert result.representative_map() == {"a": "a", "b": "a"}


def test_large_temporal_gap_not_stitched() -> None:
    nodes = [
        _node("a", 0, 10, (0, 0), (50, 0), vel=(5, 0)),
        _node("b", 500, 520, (60, 0), (100, 0)),  # gap >> MAX_GAP_FRAMES
    ]
    result = MinCostFlowStitcher().stitch(nodes)
    assert len(result.groups) == 2


def test_spatially_inconsistent_fragments_not_stitched() -> None:
    # "b" appears far from where "a" was heading -> link cost gated out.
    nodes = [
        _node("a", 0, 10, (0, 0), (50, 0), vel=(5, 0)),
        _node("b", 12, 20, (4000, 4000), (4050, 4000)),
    ]
    result = MinCostFlowStitcher().stitch(nodes)
    assert len(result.groups) == 2


def test_three_fragments_chain_into_one_identity() -> None:
    nodes = [
        _node("a", 0, 9, (0, 0), (45, 0), vel=(5, 0)),
        _node("b", 11, 19, (55, 0), (95, 0), vel=(5, 0)),
        _node("c", 21, 29, (105, 0), (145, 0), vel=(5, 0)),
    ]
    result = MinCostFlowStitcher().stitch(nodes)
    assert len(result.groups) == 1
    assert sorted(result.groups[0]) == ["a", "b", "c"]


def test_two_independent_players_kept_separate() -> None:
    # Two players running parallel lanes, each fragmented once. Stitching should
    # produce exactly two identities, each chaining its own two fragments.
    nodes = [
        _node("a1", 0, 9, (0, 0), (45, 0), vel=(5, 0)),
        _node("a2", 11, 19, (55, 0), (95, 0), vel=(5, 0)),
        _node("b1", 0, 9, (0, 500), (45, 500), vel=(5, 0)),
        _node("b2", 11, 19, (55, 500), (95, 500), vel=(5, 0)),
    ]
    result = MinCostFlowStitcher().stitch(nodes)
    assert len(result.groups) == 2
    groups = sorted(sorted(g) for g in result.groups)
    assert groups == [["a1", "a2"], ["b1", "b2"]]
