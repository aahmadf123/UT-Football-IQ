"""Versioned coaching definitions; availability never implies model approval."""

from typing import Any

DEFINITION_VERSION = "2026-10-21.1"

# Scalar key is explicit: a multidimensional packet is never averaged wholesale.
_SPECS = [
    (
        "protection_envelope_width",
        "Observed envelope width",
        "Lateral y-span of an explicitly measured source-shot protection polygon. "
        "Scalar is the maximum observed span in the declared measurement window; "
        "the polygon does not establish protection success.",
        "yd",
        "width_yards",
        [
            "qualified_measured_polygon",
            "calibrated_field_vertices",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "protection_envelope_depth",
        "Observed envelope depth",
        "Longitudinal x-span of an explicitly measured source-shot protection polygon; "
        "maximum observed span within the declared measurement window.",
        "yd",
        "depth_yards",
        [
            "qualified_measured_polygon",
            "calibrated_field_vertices",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "protection_envelope_area",
        "Observed envelope area",
        "Shoelace area of an explicitly measured simple polygon; maximum observed "
        "area within the declared measurement window. No unseen bodies or convex "
        "hull are inferred.",
        "yd²",
        "area_square_yards",
        [
            "qualified_measured_polygon",
            "calibrated_field_vertices",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "protection_envelope_compression",
        "Observed envelope compression",
        "Signed polygon area decrease divided by native elapsed time. Positive means "
        "shrinking, negative means expanding. Scalar is the interval mean, not a protection grade.",
        "yd²/s",
        "square_yards_per_second",
        [
            "qualified_measured_polygon",
            "calibrated_field_vertices",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "lineman_first_second_displacement",
        "Original lineman first-second displacement",
        "Net snap-to-one-second displacement for an original named lineman, with signed "
        "forward and lateral components. Partial source prefixes remain evidence only.",
        "yd",
        "yards",
        [
            "original_lineman_roles",
            "qualified_snap_los_direction",
            "calibrated_trajectory",
            "native_source_timestamps",
            "same_known_shot",
        ],
        "practice_game",
    ),
    (
        "observed_pass_set_geometry",
        "Observed signed pass-set geometry",
        "Signed depth behind the qualified original LOS in the first second of explicit "
        "pass-protection participation; positive is behind LOS, negative is ahead. "
        "Includes signed net displacement without inferring protection success.",
        "yd",
        "depth_yards",
        [
            "original_lineman_roles",
            "qualified_snap_los_direction",
            "qualified_pass_protection_participation",
            "calibrated_trajectory",
            "native_source_timestamps",
        ],
        "practice_game",
    ),
    (
        "observed_route_depth",
        "Observed signed route depth",
        "Maximum signed forward position relative to the qualified original LOS during "
        "explicit WR/TE route participation. Negative depths remain negative.",
        "yd",
        "yards",
        [
            "resolved_receiver_role",
            "qualified_routes_participation",
            "qualified_snap_los_direction",
            "calibrated_trajectory",
            "native_source_timestamps",
        ],
        "practice_game",
    ),
    (
        "receiver_snap_alignment",
        "Observed receiver snap split",
        "Signed lateral split from the explicit original center at snap, positive to "
        "offensive left. Signed behind-LOS depth is retained separately; "
        "alignment subtype is not inferred.",
        "yd",
        "lateral_yards",
        [
            "resolved_receiver_role",
            "original_center",
            "qualified_snap_los_direction",
            "calibrated_positions",
            "same_known_shot",
        ],
        "practice_game",
    ),
    (
        "receiver_release_location",
        "Observed receiver release location",
        "Explicit qualified receiver-release actor location in calibrated field coordinates; "
        "no release point is inferred from path shape.",
        "field_location",
        None,
        [
            "resolved_receiver_role",
            "qualified_routes_participation",
            "qualified_receiver_release",
            "exact_actor_event_id",
            "calibrated_location",
            "same_known_shot",
        ],
        "practice_game",
    ),
    (
        "receiver_break_location",
        "Observed route break location",
        "Explicit qualified route-break actor location in calibrated field coordinates; "
        "no break point or route name is inferred from path shape.",
        "field_location",
        None,
        [
            "resolved_receiver_role",
            "qualified_routes_participation",
            "qualified_route_break",
            "exact_actor_event_id",
            "calibrated_location",
            "same_known_shot",
        ],
        "practice_game",
    ),
    (
        "position_opportunity",
        "Qualified position opportunity",
        "One explicit qualified position participation for a resolved player and "
        "opportunity kind, including reps without measurable outcomes. Canonical "
        "plays count once per player and kind; unknown identity is unavailable.",
        "rep",
        "value",
        [
            "qualified_position_participation",
            "resolved_player",
            "resolved_role",
            "explicit_team",
            "native_source_timestamps",
        ],
        "practice_game",
    ),
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
        [
            "snap",
            "resolved_qb",
            "continuous_qualified_pressure_coverage",
            "endpoint_or_censor_time",
        ],
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
        "Observed distance through time to an explicit comparison opponent or the "
        "nearest observed defender; the scalar is the minimum observed distance. "
        "Coverage responsibility is not inferred.",
        "yd",
        "yards",
        [
            "qualified_position_participation",
            "calibrated_positions",
            "comparison_opponent",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
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
        "width_yards",
        [
            "original_lineman_roles",
            "measured_field_body_extents",
            "complete_defender_occupancy",
            "los_cross_section",
            "width_validation",
        ],
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
        "signed_forward_gain",
        "Signed forward gain",
        "Observed carrier endpoint relative to the original LOS along the measured "
        "attacking direction; negative values are retained. "
        "This is not official ball-spot yardage.",
        "yd",
        "yards",
        [
            "identified_carrier_possession",
            "qualified_los",
            "attacking_direction",
            "qualified_terminal_event",
            "continuous_trajectory",
        ],
        "practice_game",
    ),
    (
        "actual_entry_gap",
        "Observed entry gap",
        "One observed carrier LOS crossing, labeled by the original formation-relative "
        "gap. The intended gap is not inferred.",
        "rep",
        "value",
        [
            "identified_carrier",
            "original_lineman_roles",
            "qualified_los",
            "attacking_direction",
            "continuous_los_crossing",
        ],
        "practice_game",
    ),
    (
        "pre_contact_yards",
        "Signed pre-contact yards",
        "Observed first-contact carrier position minus original LOS along the attacking direction.",
        "yd",
        "yards",
        [
            "identified_carrier_possession",
            "qualified_first_contact",
            "qualified_los",
            "attacking_direction",
            "continuous_trajectory",
        ],
        "practice_game",
    ),
    (
        "post_contact_yards",
        "Signed post-contact yards",
        "Observed carrier endpoint minus qualified first-contact position "
        "along the attacking direction.",
        "yd",
        "yards",
        [
            "identified_carrier_possession",
            "qualified_first_contact",
            "qualified_terminal_event",
            "attacking_direction",
            "continuous_trajectory",
        ],
        "practice_game",
    ),
    (
        "chip_to_route_delay",
        "Chip-to-route delay",
        "Qualified route-start timestamp minus chip-end timestamp "
        "for the same observed interaction and player.",
        "s",
        "seconds",
        [
            "qualified_chip_end",
            "qualified_route_start",
            "same_interaction_id",
            "resolved_actor",
            "source_timestamps",
        ],
        "practice_game",
    ),
    (
        "get_off_time",
        "Get-off time",
        "First observed movement timestamp minus snap; no recognition or assignment judgment.",
        "s",
        "seconds",
        [
            "qualified_position_participation",
            "snap",
            "qualified_first_movement",
            "resolved_role",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "observed_rush_path",
        "Observed rush path",
        "Measured path length and trajectory during explicit pass-rush participation. "
        "Movement does not establish pressure or assignment correctness.",
        "yd",
        "yards",
        [
            "qualified_position_participation",
            "pass_rush",
            "resolved_defensive_role",
            "calibrated_positions",
            "native_source_timestamps",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "observed_pursuit_path",
        "Observed pursuit path",
        "Measured path length and trajectory during explicit pursuit participation; "
        "no tackling or assignment quality is inferred.",
        "yd",
        "yards",
        [
            "qualified_position_participation",
            "pursuit",
            "resolved_defensive_role",
            "calibrated_positions",
            "native_source_timestamps",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "observed_closing_speed",
        "Observed closing speed",
        "Signed rate of decrease in distance to an explicit comparison opponent; "
        "positive means closing, negative means opening. The scalar is the interval "
        "mean. This does not measure recognition, tackling or responsibility.",
        "yd/s",
        "yards_per_second",
        [
            "qualified_position_participation",
            "explicit_comparison_opponent",
            "calibrated_paired_trajectories",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "operation_time",
        "Observed operation time",
        "Qualified kick or punt timestamp minus snap for the same explicit kicking operation.",
        "s",
        "seconds",
        [
            "qualified_position_participation",
            "qualified_snap",
            "qualified_kick_or_punt",
            "same_operation_id",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "hang_time",
        "Observed hang time",
        "Qualified landing timestamp minus kick or punt timestamp for the same "
        "continuously observed ball and operation.",
        "s",
        "seconds",
        [
            "qualified_position_participation",
            "qualified_kick_or_punt",
            "qualified_landing",
            "same_operation_id",
            "same_ball_id",
            "native_source_timestamps",
            "same_known_shot",
            "continuous_ball_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "kick_landing_location",
        "Observed kick/punt landing location",
        "Qualified field-coordinate landing point for an explicit continuously "
        "observed kick or punt operation. This spatial packet has no scalar summary.",
        "field_location",
        None,
        [
            "qualified_position_participation",
            "qualified_landing",
            "same_operation_id",
            "same_ball_id",
            "calibrated_landing_location",
            "continuous_ball_visibility_200ms",
        ],
        "practice_game",
    ),
    (
        "te_usage_transition",
        "Observed TE usage transition",
        "Explicit qualified block/chip/route state events for one tight end and "
        "interaction; seconds measures the first block/chip observation to route start.",
        "s",
        "seconds",
        [
            "qualified_position_participation",
            "qualified_te_usage_events",
            "same_interaction_id",
            "resolved_te_role",
            "explicit_interaction_opponent",
            "same_known_shot",
            "continuous_visibility_200ms",
        ],
        "practice_game",
    ),
    *[
        (
            name,
            label,
            f"Experimental {label.lower()} at a source-clock prediction snapshot. "
            "Training, calibration and held-out evaluation use independent source groups; "
            "future events are excluded from the feature snapshot. "
            "No approved staff model is bundled.",
            unit,
            "value",
            [
                "heldout_evaluation",
                "model_approval",
                "artifact_bound_feature_snapshot",
                "source_clock",
                "reviewed_outcome_labels",
            ],
            "experimental",
        )
        for name, label, unit in [
            ("expected_completion", "Completion probability", "prob"),
            ("expected_rushing_gain", "Expected rushing gain", "yd"),
            ("expected_yac", "Expected yards after catch", "yd"),
            ("contextual_pressure", "Contextual pressure probability", "prob"),
        ]
    ],
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


_SPECS.extend(
    [
        (
            "observed_target",
            "Reviewed pass target",
            "Explicit reviewed aimed forward-pass target; no nearest-player inference.",
            "rep",
            "count",
            ["reviewed_target", "resolved_actor", "source_timestamps"],
            "practice_game",
        ),
        (
            "target_catch_outcome",
            "Reviewed target catch outcome",
            "Completed=1 or incomplete/intercepted=0 only from an explicit reviewed "
            "pass result for this target.",
            "boolean",
            "completed",
            ["reviewed_target", "reviewed_pass_result", "resolved_actor", "source_timestamps"],
            "practice_game",
        ),
        (
            "contested_target_outcome",
            "Reviewed contested-target catch outcome",
            "Catch outcome among targets explicitly reviewed as contested; "
            "proximity does not establish contest.",
            "boolean",
            "completed",
            [
                "reviewed_target",
                "reviewed_pass_result",
                "reviewed_contest",
                "resolved_actor",
                "source_timestamps",
            ],
            "practice_game",
        ),
        (
            "coverage_target_outcome",
            "Reviewed responsible defender target outcome",
            "Pass outcome assigned only to an explicitly reviewed responsible defender, "
            "never nearest-defender distance.",
            "boolean",
            "completed",
            [
                "reviewed_target",
                "reviewed_pass_result",
                "reviewed_responsibility",
                "resolved_actor",
                "source_timestamps",
            ],
            "practice_game",
        ),
        (
            "observed_yac",
            "Observed yards after catch",
            "Signed forward net advance from an observed offensive forward-pass reception "
            "to the same possession's explicit terminal event; preserves negative advance "
            "and is not path length.",
            "yd",
            "yards",
            [
                "reviewed_reception",
                "reviewed_possession_end",
                "continuous_tracking",
                "attacking_direction",
                "field_calibration",
            ],
            "practice_game",
        ),
        (
            "observed_cut_location",
            "Reviewed carrier cut location",
            "Measured location of an explicitly reviewed carrier cut during known possession; "
            "no cut or success is inferred from motion.",
            "field_location",
            None,
            [
                "reviewed_cut",
                "reviewed_possession",
                "resolved_actor",
                "field_calibration",
                "source_timestamps",
            ],
            "practice_game",
        ),
        (
            "observed_tackle",
            "Reviewed made tackle",
            "Explicit reviewed made-tackle event for the named defender and carrier.",
            "count",
            "count",
            ["reviewed_tackle_outcome", "resolved_actor_pair", "source_timestamps"],
            "practice_game",
        ),
        (
            "observed_tackle_assist",
            "Reviewed tackle assist",
            "Explicit reviewed assist event for the named defender and carrier; "
            "separate from a made solo tackle.",
            "count",
            "count",
            ["reviewed_tackle_outcome", "resolved_actor_pair", "source_timestamps"],
            "practice_game",
        ),
        (
            "observed_missed_tackle",
            "Reviewed missed tackle",
            "Explicit reviewed missed tackle attributed to the named defender; "
            "movement after contact is insufficient.",
            "count",
            "count",
            ["reviewed_tackle_outcome", "resolved_actor_pair", "source_timestamps"],
            "practice_game",
        ),
        (
            "observed_forced_missed_tackle",
            "Reviewed carrier-forced missed tackle",
            "A reviewed missed tackle additionally explicitly attributed to the carrier; "
            "missed alone does not establish carrier causation.",
            "count",
            "count",
            [
                "reviewed_tackle_outcome",
                "reviewed_carrier_causation",
                "resolved_actor_pair",
                "source_timestamps",
            ],
            "practice_game",
        ),
    ]
)


_SPECS.extend(
    [
        (
            "gap_clear_timeline",
            "Measured LOS lane opening/closure intervals",
            "Discrete original-gap LOS clear widths, with interval-censored changes and "
            "left/right censoring. Opening is strictly width greater than the explicit "
            "threshold (default zero yards); no continuous open route or blocking success "
            "is claimed.",
            "source_interval",
            None,
            [
                "original_lineman_roles",
                "qualified_snap",
                "qualified_los",
                "measured_field_body_extents",
                "complete_defender_occupancy",
                "source_timestamps",
            ],
            "practice_game",
        ),
        (
            "clear_lane_width_at_handoff",
            "LOS clear width at observed handoff",
            "Largest measured contiguous free LOS section in the original gap at the exact "
            "reviewed handoff clock. Occupancy is never interpolated to a missing "
            "event-time sample.",
            "yd",
            "width_yards",
            [
                "qualified_handoff",
                "identified_carrier",
                "original_lineman_roles",
                "measured_field_body_extents",
                "complete_defender_occupancy",
                "field_calibration",
                "source_timestamps",
            ],
            "practice_game",
        ),
        (
            "clear_lane_width_at_entry",
            "LOS clear width at actual entry",
            "Measured clear LOS section in the runner's original named entry gap at the "
            "exact trajectory crossing clock; preserves unavailable widths when no geometry "
            "snapshot exists there.",
            "yd",
            "width_yards",
            [
                "identified_carrier",
                "continuous_los_crossing",
                "original_lineman_roles",
                "measured_field_body_extents",
                "complete_defender_occupancy",
                "field_calibration",
                "source_timestamps",
            ],
            "practice_game",
        ),
    ]
)


def metric_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "label": label,
            "definition": definition,
            "unit": unit,
            "version": DEFINITION_VERSION,
            "value_kind": "predicted"
            if name
            in {
                "xpressure",
                "expected_completion",
                "expected_rushing_gain",
                "expected_yac",
                "contextual_pressure",
            }
            else "observed",
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
