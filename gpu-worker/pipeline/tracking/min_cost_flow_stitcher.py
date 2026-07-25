"""Min-cost-flow track stitching (Issue #131, nightly only).

Across a full play, short tracklets that belong to the *same* player get
fragmented by occlusion and missed detections. We stitch them back together
by formulating the link problem as a min-cost flow over a graph whose nodes
are tracklets and whose edges encode appearance + motion + temporal-gap costs
(Zhang et al., CVPR 2008 / the CVPR 2015 pairwise-cost formulation cited in
the issue). It is solved here with a successive-shortest-path solver in pure
Python — ``OR-Tools`` / ``LPSolve`` are not worker dependencies.

**Single-camera, within-clip only** (Issue #101). The graph is built from the
tracklets of one clip; there is no cross-camera node set and no global
identity store. The output is groups of tracklet ids that share an identity;
the caller assigns each group the *same* ``player_id`` through the existing
PATCH path, so **no tracklet schema changes** and existing per-tracklet
outputs are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import structlog

log = structlog.get_logger(__name__)

# Default edge-cost weights and gating.
W_MOTION = 1.0
W_APPEARANCE = 1.0
MAX_GAP_FRAMES = 60  # tracklets separated by more than this are never linked
MAX_LINK_COST = 1.5  # transitions above this are not even added as edges
# Entry/exit cost of *starting a fresh chain*. Stitching two tracklets pays one
# (entry + exit) once instead of twice, so a link is preferred whenever its
# transition cost is below ``2 · ENTRY_EXIT_COST``. With ENTRY_EXIT_COST = 1.0
# that bound (2.0) sits above ``MAX_LINK_COST`` (1.5), so the link gate — not
# the flow arithmetic — is what decides a merge.
ENTRY_EXIT_COST = 1.0
# Reward for including a tracklet at all (negative cost). Must exceed
# ``2 · ENTRY_EXIT_COST`` so even an isolated tracklet is worth a chain of its
# own (entry + reward + exit < 0).
OBSERVATION_REWARD = 3.0


@dataclass
class TrackletNode:
    """Minimal tracklet summary needed for stitching."""

    tracklet_id: str
    start_frame: int
    end_frame: int
    start_position: tuple[float, float]
    end_position: tuple[float, float]
    end_velocity: tuple[float, float] = (0.0, 0.0)
    embedding: np.ndarray | None = None


@dataclass
class _Edge:
    to: int
    cap: int
    cost: float
    flow: int = 0


class _MinCostFlow:
    """Successive-shortest-path min-cost flow (SPFA for negative edges).

    The stitching graph is a temporal DAG (edges only move forward in time),
    so there are no negative cycles and Bellman-Ford/SPFA is safe.
    """

    def __init__(self, n: int) -> None:
        self.n = n
        self.graph: list[list[int]] = [[] for _ in range(n)]
        self.edges: list[_Edge] = []

    def add_edge(self, u: int, v: int, cap: int, cost: float) -> None:
        self.graph[u].append(len(self.edges))
        self.edges.append(_Edge(v, cap, cost))
        self.graph[v].append(len(self.edges))
        self.edges.append(_Edge(u, 0, -cost))

    def _shortest_augmenting_path(self, s: int, t: int) -> list[int] | None:
        INF = float("inf")
        dist = [INF] * self.n
        in_queue = [False] * self.n
        prev_edge = [-1] * self.n
        prev_node = [-1] * self.n
        dist[s] = 0.0
        queue = [s]
        in_queue[s] = True
        while queue:
            u = queue.pop(0)
            in_queue[u] = False
            for eid in self.graph[u]:
                e = self.edges[eid]
                if e.cap - e.flow > 0 and dist[u] + e.cost < dist[e.to] - 1e-12:
                    dist[e.to] = dist[u] + e.cost
                    prev_edge[e.to] = eid
                    prev_node[e.to] = u
                    if not in_queue[e.to]:
                        queue.append(e.to)
                        in_queue[e.to] = True
        if dist[t] >= -1e-12:
            # No augmenting path that further reduces total cost.
            return None
        path: list[int] = []
        v = t
        while v != s:
            eid = prev_edge[v]
            if eid == -1:
                return None
            path.append(eid)
            v = prev_node[v]
        path.reverse()
        return path

    def solve(self, s: int, t: int) -> None:
        """Push unit flow along negative-cost shortest paths until none remain."""
        while True:
            path = self._shortest_augmenting_path(s, t)
            if path is None:
                break
            for eid in path:
                self.edges[eid].flow += 1
                self.edges[eid ^ 1].flow -= 1


@dataclass
class StitchResult:
    """Stitching output: groups of tracklet ids that share an identity."""

    groups: list[list[str]] = field(default_factory=list)

    def representative_map(self) -> dict[str, str]:
        """Map every tracklet id to its group's representative (earliest) id."""
        mapping: dict[str, str] = {}
        for group in self.groups:
            rep = group[0]
            for tid in group:
                mapping[tid] = rep
        return mapping


class MinCostFlowStitcher:
    """Stitch fragmented tracklets within one clip via min-cost flow."""

    def __init__(
        self,
        max_gap_frames: int = MAX_GAP_FRAMES,
        max_link_cost: float = MAX_LINK_COST,
        position_scale: float = 300.0,
        observation_reward: float = OBSERVATION_REWARD,
        entry_exit_cost: float = ENTRY_EXIT_COST,
    ) -> None:
        self.max_gap_frames = max_gap_frames
        self.max_link_cost = max_link_cost
        self.position_scale = max(1e-6, position_scale)
        self.observation_reward = observation_reward
        self.entry_exit_cost = entry_exit_cost

    def _link_cost(self, a: TrackletNode, b: TrackletNode) -> float | None:
        gap = b.start_frame - a.end_frame
        if gap <= 0 or gap > self.max_gap_frames:
            return None
        px = a.end_position[0] + a.end_velocity[0] * gap
        py = a.end_position[1] + a.end_velocity[1] * gap
        dist = float(np.hypot(b.start_position[0] - px, b.start_position[1] - py))
        motion = dist / self.position_scale
        if a.embedding is not None and b.embedding is not None:
            appearance = 1.0 - float(np.dot(a.embedding, b.embedding))
        else:
            appearance = 0.5  # neutral when appearance is unavailable
        cost = W_MOTION * motion + W_APPEARANCE * appearance
        return cost if cost <= self.max_link_cost else None

    def stitch(self, tracklets: list[TrackletNode]) -> StitchResult:
        """Return tracklet-id groups; each group is one stitched identity."""
        if not tracklets:
            return StitchResult(groups=[])
        order = sorted(range(len(tracklets)), key=lambda i: tracklets[i].start_frame)
        ts = [tracklets[i] for i in order]
        n = len(ts)

        # Node ids: source=0, sink=1, then pre_i = 2+2i, post_i = 3+2i.
        source, sink = 0, 1
        mcf = _MinCostFlow(2 + 2 * n)

        def pre(i: int) -> int:
            return 2 + 2 * i

        def post(i: int) -> int:
            return 3 + 2 * i

        for i in range(n):
            mcf.add_edge(source, pre(i), 1, self.entry_exit_cost)  # enter a chain
            mcf.add_edge(pre(i), post(i), 1, -self.observation_reward)  # include
            mcf.add_edge(post(i), sink, 1, self.entry_exit_cost)  # exit a chain
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                cost = self._link_cost(ts[i], ts[j])
                if cost is not None:
                    mcf.add_edge(post(i), pre(j), 1, cost)

        mcf.solve(source, sink)

        # Decompose the flow into chains by following saturated transition edges.
        next_of: dict[int, int] = {}
        for i in range(n):
            for eid in mcf.graph[post(i)]:
                e = mcf.edges[eid]
                if e.flow > 0 and e.to != sink and (e.to - 2) % 2 == 0:
                    j = (e.to - 2) // 2
                    next_of[i] = j
        has_pred = set(next_of.values())

        groups: list[list[str]] = []
        for i in range(n):
            if i in has_pred:
                continue
            chain: list[str] = []
            cur: int | None = i
            seen: set[int] = set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                chain.append(ts[cur].tracklet_id)
                cur = next_of.get(cur)
            groups.append(chain)

        log.info(
            "min_cost_flow_stitched",
            tracklets=n,
            identities=len(groups),
            merged=n - len(groups),
        )
        return StitchResult(groups=groups)
