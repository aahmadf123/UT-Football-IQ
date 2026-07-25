"""Ball state machine (Issue #134).

Turns the smoothed ball track + player tracklets into a legal sequence of ball
states and the play events they imply:

    PRE_SNAP → SNAP → (CARRIED | THROWN)
        THROWN:  IN_AIR → (CAUGHT | INCOMPLETE | INT | FUMBLE)
        CARRIED: RUNNING → (TACKLE | OOB | TD | FUMBLE)

Transitions (research report §3.6.3):

* ``PRE_SNAP → SNAP`` — from the snap detector (Issue #132).
* ``SNAP → THROWN`` — ball speed exceeds the running-speed threshold **and**
  the ball leaves the QB's region.
* ``IN_AIR → CAUGHT`` — ball speed drops near zero **and** the ball is inside a
  receiver's catch radius (a defender there ⇒ ``INT``; nobody ⇒ ``INCOMPLETE``).

All positions are field-plane yards; velocities are yd/s. Deterministic and
dependency-light (NumPy only).
"""

from __future__ import annotations

import enum
import math
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog

from pipeline.ball.ball_tracker import BallTrackPoint

log = structlog.get_logger(__name__)


class BallState(str, enum.Enum):
    PRE_SNAP = "PRE_SNAP"
    SNAP = "SNAP"
    CARRIED = "CARRIED"
    THROWN = "THROWN"
    IN_AIR = "IN_AIR"
    CAUGHT = "CAUGHT"
    INCOMPLETE = "INCOMPLETE"
    INT = "INT"
    FUMBLE = "FUMBLE"
    RUNNING = "RUNNING"
    TACKLE = "TACKLE"
    OOB = "OOB"
    TD = "TD"


# Legal directed transitions — used by :func:`validate`.
LEGAL_TRANSITIONS: dict[BallState, set[BallState]] = {
    BallState.PRE_SNAP: {BallState.SNAP},
    BallState.SNAP: {BallState.CARRIED, BallState.THROWN},
    BallState.THROWN: {BallState.IN_AIR},
    BallState.IN_AIR: {
        BallState.CAUGHT,
        BallState.INCOMPLETE,
        BallState.INT,
        BallState.FUMBLE,
    },
    BallState.CARRIED: {BallState.RUNNING},
    BallState.RUNNING: {
        BallState.TACKLE,
        BallState.OOB,
        BallState.TD,
        BallState.FUMBLE,
    },
    BallState.CAUGHT: set(),
    BallState.INCOMPLETE: set(),
    BallState.INT: set(),
    BallState.FUMBLE: set(),
    BallState.TACKLE: set(),
    BallState.OOB: set(),
    BallState.TD: set(),
}

# Terminal-state → event_type mapping (event_type strings stay within the
# documented label taxonomy; see docs/label-taxonomy-v1.md).
_STATE_EVENT: dict[BallState, str] = {
    BallState.SNAP: "snap",
    BallState.THROWN: "throw",
    BallState.CAUGHT: "catch",
    BallState.INCOMPLETE: "incomplete",
    BallState.INT: "interception",
    BallState.FUMBLE: "fumble",
    BallState.TACKLE: "tackle",
    BallState.OOB: "out_of_bounds",
    BallState.TD: "touchdown",
}

# Thresholds.
THROW_SPEED_YD_S: float = 12.0   # ball faster than a sprinting carrier
CATCH_SPEED_YD_S: float = 5.0    # ball "settles" into hands
POSSESSION_YD: float = 2.0       # ball within this of a player ⇒ possession
QB_LEAVE_YD: float = 3.0         # ball this far from QB ⇒ has left the pocket
FUMBLE_SPEED_YD_S: float = 14.0  # abrupt high-speed separation during a carry
FIELD_HALF_WIDTH_YD: float = 26.65  # NCAA hash-to-sideline (53⅓ yd / 2)


@dataclass(frozen=True)
class StateTransition:
    frame: int
    from_state: BallState
    to_state: BallState
    event_type: str | None


@dataclass
class StateMachineResult:
    transitions: list[StateTransition]
    final_state: BallState
    throw_frame: int | None = None
    catch_frame: int | None = None
    first_airborne_frame: int | None = None
    attribution: dict[str, str | None] = field(default_factory=dict)

    @property
    def states(self) -> list[BallState]:
        return [t.to_state for t in self.transitions]

    def events(self) -> list[dict[str, object]]:
        """Transitions that carry a play event, ready for the events stage."""
        return [
            {"event_type": t.event_type, "frame_number": t.frame, "state": t.to_state.value}
            for t in self.transitions
            if t.event_type is not None
        ]


def _nearest_player(
    point: tuple[float, float],
    players: dict[str, tuple[float, float]],
) -> tuple[str | None, float]:
    best_id: str | None = None
    best_d = math.inf
    for tid, (px, py) in players.items():
        d = math.hypot(px - point[0], py - point[1])
        if d < best_d:
            best_id, best_d = tid, d
    return best_id, best_d


def validate(states: Iterable[BallState]) -> bool:
    """True if every consecutive pair is a legal transition."""
    seq = list(states)
    for a, b in zip(seq[:-1], seq[1:]):
        if b not in LEGAL_TRANSITIONS.get(a, set()):
            return False
    return True


class BallStateMachine:
    """Drive the ball through its states from snap to a terminal event."""

    def __init__(
        self,
        *,
        throw_speed_yd_s: float = THROW_SPEED_YD_S,
        catch_speed_yd_s: float = CATCH_SPEED_YD_S,
        possession_yd: float = POSSESSION_YD,
        qb_leave_yd: float = QB_LEAVE_YD,
        field_half_width_yd: float = FIELD_HALF_WIDTH_YD,
    ) -> None:
        self.throw_speed = throw_speed_yd_s
        self.catch_speed = catch_speed_yd_s
        self.possession_yd = possession_yd
        self.qb_leave_yd = qb_leave_yd
        self.field_half_width = field_half_width_yd

    def run(
        self,
        snap_frame: int,
        ball_track: list[BallTrackPoint],
        player_positions: dict[int, dict[str, tuple[float, float]]],
        *,
        qb_id: str | None = None,
        defender_ids: set[str] | None = None,
        end_of_play_frame: int | None = None,
        goal_line_x: float | None = None,
    ) -> StateMachineResult:
        defenders = defender_ids or set()
        by_frame = {p.frame: p for p in ball_track}
        post = [p for p in ball_track if p.frame >= snap_frame]

        transitions = [
            StateTransition(snap_frame, BallState.PRE_SNAP, BallState.SNAP, "snap")
        ]
        if not post:
            return StateMachineResult(transitions, BallState.SNAP)

        qb_pos_at_snap = (
            player_positions.get(snap_frame, {}).get(qb_id) if qb_id else None
        )

        # ── SNAP → THROWN | CARRIED ────────────────────────────────────────
        # ``release_frame`` is the first fast frame (still near the QB → used
        # for throw attribution); ``throw_frame`` additionally requires the
        # ball to have left the QB region (the THROWN state transition).
        release_frame: int | None = None
        throw_frame: int | None = None
        for p in post:
            fast = p.speed > self.throw_speed
            if fast and release_frame is None:
                release_frame = p.frame
            left_qb = True
            if qb_pos_at_snap is not None:
                left_qb = (
                    math.hypot(p.x - qb_pos_at_snap[0], p.y - qb_pos_at_snap[1])
                    > self.qb_leave_yd
                )
            if fast and left_qb:
                throw_frame = p.frame
                break

        if throw_frame is not None:
            first_airborne = release_frame if release_frame is not None else throw_frame
            transitions.append(
                StateTransition(throw_frame, BallState.SNAP, BallState.THROWN, "throw")
            )
            transitions.append(
                StateTransition(throw_frame, BallState.THROWN, BallState.IN_AIR, None)
            )
            result = self._resolve_air(
                throw_frame, post, player_positions, defenders, by_frame
            )
            transitions.extend(result)
            final = transitions[-1].to_state
            return StateMachineResult(
                transitions,
                final,
                throw_frame=throw_frame,
                catch_frame=transitions[-1].frame if final in {
                    BallState.CAUGHT,
                    BallState.INT,
                } else None,
                first_airborne_frame=first_airborne,
            )

        # ── SNAP → CARRIED → RUNNING → terminal ────────────────────────────
        carry_frame = post[0].frame
        transitions.append(
            StateTransition(carry_frame, BallState.SNAP, BallState.CARRIED, None)
        )
        transitions.append(
            StateTransition(carry_frame, BallState.CARRIED, BallState.RUNNING, None)
        )
        transitions.append(
            self._resolve_run(post, end_of_play_frame, goal_line_x, snap_frame)
        )
        return StateMachineResult(transitions, transitions[-1].to_state)

    def _resolve_air(
        self,
        throw_frame: int,
        post: list[BallTrackPoint],
        player_positions: dict[int, dict[str, tuple[float, float]]],
        defenders: set[str],
        by_frame: dict[int, BallTrackPoint],
    ) -> list[StateTransition]:
        airborne = [p for p in post if p.frame > throw_frame]
        for p in airborne:
            if p.speed > self.catch_speed:
                continue
            players = player_positions.get(p.frame, {})
            who, dist = _nearest_player((p.x, p.y), players)
            if who is not None and dist <= self.possession_yd:
                state = BallState.INT if who in defenders else BallState.CAUGHT
                return [
                    StateTransition(
                        p.frame, BallState.IN_AIR, state, _STATE_EVENT[state]
                    )
                ]
            # Ball settled with nobody there ⇒ incomplete.
            return [
                StateTransition(
                    p.frame,
                    BallState.IN_AIR,
                    BallState.INCOMPLETE,
                    _STATE_EVENT[BallState.INCOMPLETE],
                )
            ]
        # Never settled within the clip: best-effort incomplete at last frame.
        last = airborne[-1] if airborne else by_frame[throw_frame]
        return [
            StateTransition(
                last.frame,
                BallState.IN_AIR,
                BallState.INCOMPLETE,
                _STATE_EVENT[BallState.INCOMPLETE],
            )
        ]

    def _resolve_run(
        self,
        post: list[BallTrackPoint],
        end_of_play_frame: int | None,
        goal_line_x: float | None,
        snap_frame: int,
    ) -> StateTransition:
        # Detect an abrupt high-speed separation ⇒ fumble.
        for p in post:
            if p.speed > FUMBLE_SPEED_YD_S:
                return StateTransition(
                    p.frame, BallState.RUNNING, BallState.FUMBLE,
                    _STATE_EVENT[BallState.FUMBLE],
                )
        end_frame = end_of_play_frame if end_of_play_frame is not None else post[-1].frame
        end_pt = next((p for p in reversed(post) if p.frame <= end_frame), post[-1])
        if goal_line_x is not None and end_pt.x >= goal_line_x:
            state = BallState.TD
        elif abs(end_pt.y) > self.field_half_width:
            state = BallState.OOB
        else:
            state = BallState.TACKLE
        return StateTransition(
            end_pt.frame, BallState.RUNNING, state, _STATE_EVENT[state]
        )
