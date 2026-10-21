"""Which jersey colour is the offence on this play.

The colour classifier finds two team clusters (plus officials) but colour
alone cannot say which one has the ball. It used to call the cluster further
from the grass colour "offense", which on any given game is the same team
every snap: Toledo in white at Michigan State was "offense" on all of its
defensive plays too.

Two independent kinds of evidence decide it instead, in this order:

1. **Formation** (per play, needs field coordinates). At an aligned
   formation the defence fields a secondary and the offence does not, so the
   side of the line of scrimmage with the deeper players is the defence
   (:func:`pipeline.homography.field_orientation.estimate_attack_direction`).
   The team cluster standing mostly on that side is the defence.
2. **Possession and jerseys** (per game). The official play-by-play says
   whether Toledo has the ball, and college teams default to dark jerseys at
   home and white on the road. The lighter cluster is the road team.

When neither applies (special teams, an unlinked play, one team cluster) the
sides stay unknown: a wrong "offense" corrupts formation, personnel and the
Toledo-only physical metrics, an honest unknown does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from pipeline.homography.field_orientation import estimate_attack_direction

TEAM_OFFENSE = "offense"
TEAM_DEFENSE = "defense"

#: Frames whose formation separated must agree this strongly to decide a play.
FORMATION_AGREEMENT = 0.75
#: ...and there must be at least this many of them.
FORMATION_MIN_FRAMES = 3


@dataclass(frozen=True)
class SideContext:
    """What the official record says about this play."""

    #: ``offense`` / ``defense`` when Toledo's side is known, else ``None``
    #: (special teams, or a play not matched to the play-by-play).
    toledo_on: str | None = None
    #: Whether Toledo wears the lighter jersey (True on the road).
    toledo_light: bool | None = None


@dataclass(frozen=True)
class SideDecision:
    offense_cluster: int | None
    evidence: str
    frames_resolved: int = 0
    agreement: float | None = None


def formation_offense_cluster(
    tracklets: list[dict[str, Any]],
    cluster_by_tracklet: dict[str, int],
    team_clusters: tuple[int, int],
    frames: list[int],
) -> SideDecision:
    """Vote over frames: the cluster on the deeper side of the ball defends."""
    by_frame: dict[int, list[tuple[float, float, int]]] = {f: [] for f in frames}
    wanted = set(frames)
    for track in tracklets:
        cluster = cluster_by_tracklet.get(str(track.get("id") or track.get("tracklet_id") or ""))
        if cluster not in team_clusters:
            continue
        for point in track.get("track_points", []):
            frame = point.get("frame_number")
            x, y = point.get("field_x"), point.get("field_y")
            if frame in wanted and x is not None and y is not None:
                by_frame[int(frame)].append((float(x), float(y), int(cluster)))

    votes: list[int] = []
    for players in by_frame.values():
        if not players:
            continue
        estimate = estimate_attack_direction([(x, y) for x, y, _ in players])
        if not estimate.resolved or estimate.los_x is None or estimate.direction is None:
            continue
        # The offence attacks toward ``direction``: the defence lines up there.
        defence_share: dict[int, list[bool]] = {c: [] for c in team_clusters}
        for x, _, cluster in players:
            if abs(x - estimate.los_x) > 0.5:
                defence_share[cluster].append((x - estimate.los_x) * estimate.direction > 0)
        shares = {c: (np.mean(v) if v else None) for c, v in defence_share.items()}
        a, b = team_clusters
        if shares[a] is None or shares[b] is None or abs(shares[a] - shares[b]) < 0.4:
            continue  # the two colours aren't on opposite sides; no vote
        votes.append(b if shares[a] > shares[b] else a)  # the offence is the other one

    if len(votes) < FORMATION_MIN_FRAMES:
        return SideDecision(None, "formation_unresolved", frames_resolved=len(votes))
    winner = max(team_clusters, key=votes.count)
    agreement = votes.count(winner) / len(votes)
    if agreement < FORMATION_AGREEMENT:
        return SideDecision(None, "formation_disagrees", len(votes), round(agreement, 2))
    return SideDecision(winner, "formation_depth", len(votes), round(agreement, 2))


def jersey_offense_cluster(
    centroids: np.ndarray,
    team_clusters: tuple[int, int],
    context: SideContext,
) -> SideDecision:
    """Possession plus the home-dark / road-white convention."""
    if context.toledo_on not in (TEAM_OFFENSE, TEAM_DEFENSE) or context.toledo_light is None:
        return SideDecision(None, "possession_unknown")
    a, b = team_clusters
    light, dark = (a, b) if centroids[a][0] >= centroids[b][0] else (b, a)
    if abs(float(centroids[a][0]) - float(centroids[b][0])) < 25:
        # Similar lightness (two dark kits, a colour-rush game): no call.
        return SideDecision(None, "jerseys_alike")
    toledo = light if context.toledo_light else dark
    other = dark if toledo == light else light
    return SideDecision(toledo if context.toledo_on == TEAM_OFFENSE else other, "possession_jersey")


def resolve_offense_cluster(
    tracklets: list[dict[str, Any]],
    cluster_by_tracklet: dict[str, int],
    centroids: np.ndarray,
    team_clusters: tuple[int, int],
    frames: list[int],
    context: SideContext,
) -> SideDecision:
    """Formation when the play shows it, else possession and jerseys."""
    formation = formation_offense_cluster(tracklets, cluster_by_tracklet, team_clusters, frames)
    if formation.offense_cluster is not None:
        return formation
    jersey = jersey_offense_cluster(centroids, team_clusters, context)
    if jersey.offense_cluster is not None:
        return jersey
    return SideDecision(None, f"{formation.evidence}+{jersey.evidence}", formation.frames_resolved)
