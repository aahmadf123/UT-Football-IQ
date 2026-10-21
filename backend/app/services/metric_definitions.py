"""Versioned coaching definitions; availability never implies model approval."""

from typing import Any

DEFINITION_VERSION = "2026-10-21.1"

# Scalar key is explicit: a multidimensional packet is never averaged wholesale.
_SPECS = [
    (
        "pre_snap_split",
        "Pre-snap split",
        "Spacing between original adjacent linemen at the snap.",
        "yd",
        "width_yards",
        ["snap", "original_lineman_roles", "calibrated_positions"],
        "practice_game",
    ),
    (
        "geometric_spacing",
        "Geometric spacing",
        "Center-to-center spacing between original adjacent linemen at the stated time.",
        "yd",
        "width_yards",
        ["original_lineman_roles", "calibrated_positions", "timestamp"],
        "practice_game",
    ),
    (
        "time_to_throw",
        "Time to throw",
        "Forward-pass release minus snap timestamp.",
        "s",
        "seconds",
        ["snap", "forward_pass_release"],
        "practice_game",
    ),
    (
        "ball_holding_duration",
        "Ball-holding duration",
        "Snap until release, sack, or scramble initiation; endpoint retained.",
        "s",
        "seconds",
        ["snap", "possession_endpoint"],
        "practice_game",
    ),
    (
        "time_to_first_pressure",
        "Time to first pressure",
        "Snap until first qualified pressure onset, censored at release or film end.",
        "s",
        "seconds",
        ["snap", "qualified_pressure"],
        "practice_game",
    ),
    (
        "tackle_box_residence",
        "Tackle-box residence",
        "Time from snap until release, sack, or a sustained 200 ms exit from the "
        "original tackles' outer edges and LOS corridor.",
        "s",
        "seconds",
        ["snap", "tackle_edges", "los", "attacking_direction", "trajectory"],
        "practice_game",
    ),
    (
        "distance_traveled",
        "Distance traveled",
        "Path length of one selected athlete trajectory per canonical play.",
        "yd",
        "yards",
        ["calibrated_trajectory", "resolved_player"],
        "practice_game",
    ),
    (
        "max_speed",
        "Maximum speed",
        "Maximum qualified speed in the selected athlete trajectory.",
        "yd/s",
        "yards_per_second",
        ["calibrated_trajectory", "timestamps", "resolved_player"],
        "practice_game",
    ),
    (
        "dropback_depth",
        "Dropback depth",
        "Signed QB snap-to-release retreat: (snap field x minus release field x) "
        "times measured attacking direction. Positive means retreat; negative means advancement.",
        "yd",
        "yards",
        ["qb_role", "snap", "forward_pass_release", "attacking_direction", "trajectory"],
        "practice_game",
    ),
    (
        "cushion_at_snap",
        "Cushion at snap",
        "Observed receiver-defender distance at snap; no assignment responsibility implied.",
        "yd",
        "yards",
        ["snap", "calibrated_positions"],
        "practice_game",
    ),
    (
        "observed_separation",
        "Observed separation",
        "Observed distance to the stated comparison defender at the stated source time.",
        "yd",
        "yards",
        ["calibrated_positions", "comparison_defender", "timestamp"],
        "practice_game",
    ),
    (
        "route_depth",
        "Route depth",
        "Maximum forward advance from the LOS along the attacking direction.",
        "yd",
        "yards",
        ["receiver_role", "los", "attacking_direction", "trajectory"],
        "practice_game",
    ),
    (
        "gap_width",
        "Geometric spacing",
        "Center-to-center separation between original adjacent linemen. This is "
        "spacing, not clear lane width.",
        "yd",
        "yards",
        ["original_lineman_roles", "calibrated_positions", "attacking_direction"],
        "practice_game",
    ),
    (
        "clear_lane_width",
        "Clear lane width",
        "Contiguous unoccupied interval at the displayed LOS cross-section after body "
        "extents and defender occupancy.",
        "yd",
        "yards",
        ["body_extents", "defender_occupancy", "los_cross_section", "width_validation"],
        "practice_game",
    ),
    (
        "pass_set_depth",
        "Pass-set depth",
        "Observed blocker displacement behind the LOS, with source interval retained.",
        "yd",
        "depth_yards",
        ["blocker_role", "los", "attacking_direction", "trajectory"],
        "practice_game",
    ),
    (
        "rusher_proximity",
        "Rusher proximity",
        "Observed nearest-rusher distance to the quarterback; proximity does not "
        "establish football pressure.",
        "yd",
        "yards",
        ["qb_role", "rusher_role", "calibrated_positions"],
        "practice_game",
    ),
    (
        "handoff_to_los",
        "Handoff to LOS",
        "Time from handoff until runner first crosses the LOS.",
        "s",
        "seconds",
        ["handoff", "runner", "los_crossing"],
        "practice_game",
    ),
    (
        "engagement_duration",
        "Engagement duration",
        "Duration of a separately qualified blocking interaction.",
        "s",
        "seconds",
        ["validated_interaction", "interaction_start", "interaction_end"],
        "practice_game",
    ),
    (
        "get_off_time",
        "Get-off time",
        "First observed movement timestamp minus snap; no recognition or assignment judgment.",
        "s",
        "seconds",
        ["snap", "first_movement"],
        "practice_game",
    ),
    (
        "xsep",
        "Separation model output (experimental)",
        "Legacy model output; not a validated expected-outcome measure.",
        "yd",
        "yards",
        ["heldout_evaluation", "model_approval"],
        "experimental",
    ),
    (
        "xyards",
        "Post-catch movement model output (experimental)",
        "Legacy output; observed movement must not be called expected yards.",
        "yd",
        "yards",
        ["heldout_evaluation", "model_approval"],
        "experimental",
    ),
    (
        "xpressure",
        "Pressure model output (experimental)",
        "Experimental contextual pressure estimate, separate from rusher proximity.",
        "probability",
        "value",
        ["heldout_evaluation", "model_approval"],
        "experimental",
    ),
]


def metric_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "label": label,
            "definition": definition,
            "unit": unit,
            "version": DEFINITION_VERSION,
            "value_kind": "predicted" if name == "xpressure" else "observed",
            "scalar_key": key,
            "availability": "requires_qualified_evidence",
            "required_evidence": evidence,
            "applicability": ["practice", "scrimmage", "game"]
            if applicability == "practice_game"
            else ["experimental"],
        }
        for name, label, definition, unit, key, evidence, applicability in _SPECS
    ]


DEFINITIONS = {row["name"]: row for row in metric_definitions()}
