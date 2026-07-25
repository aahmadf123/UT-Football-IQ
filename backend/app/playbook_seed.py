"""Seed playbook concepts for Issue #15.

Two coach-editable starter concepts — one offensive concept and one defensive
structure — so the Film Room overlay has live content out of the box. They are
installed (idempotently) via ``POST /api/v1/playbook/concepts/seed`` and are
fully editable afterwards; nothing here is hard-coded into scoring.

Geometry in ``intended_route`` is **single-camera, frame-normalized** ``[x, y]``
in ``[0, 1]`` (origin top-left) so the existing single-camera render-layer
(#104) can draw it without any multi-angle camera switching (#101). The
``rule`` block is the deterministic execution check (see
``app.analytics.assignment_scoring``); it is deliberately separate from the
drawn route.

Colors use Toledo's palette: midnight blue ``#15397F`` and gold ``#FFB81C``.
"""

from __future__ import annotations

from typing import Any

# Offensive concept: a four-verticals shot. Three true verticals (depth-graded,
# which gracefully degrades to needs-review without calibrated tracking) plus a
# coach-graded QB progression read.
FOUR_VERTICALS: dict[str, Any] = {
    "name": "Four Verticals",
    "side": "offense",
    "structure_kind": "pass_concept",
    "description": (
        "Four-vertical stretch of a single-high or two-high shell. Verticals "
        "push to clear the deep zones; the QB works the middle-of-field read."
    ),
    "coaching_points": [
        "Bend the verticals off the safeties — landmarks, not straight lines.",
        "Backside vertical is the answer vs single-high middle-of-field-closed.",
    ],
    "assignments": [
        {
            "key": "x_vertical",
            "role": "X (backside)",
            "intent": "Release vertical and stem to the backside hash.",
            "coaching_point": "Beat press with an outside release, then stem to 14.",
            "intended_route": {
                "points": [[0.10, 0.88], [0.11, 0.5], [0.13, 0.14]],
                "color": "#FFB81C",
            },
            "rule": {"type": "depth_threshold", "params": {"min_depth_yards": 14.0}},
        },
        {
            "key": "z_vertical",
            "role": "Z (playside)",
            "intent": "Outside vertical, pin the corner deep.",
            "coaching_point": "Stack the corner's outside shoulder; don't drift to the boundary.",
            "intended_route": {
                "points": [[0.90, 0.88], [0.89, 0.5], [0.87, 0.14]],
                "color": "#FFB81C",
            },
            "rule": {"type": "depth_threshold", "params": {"min_depth_yards": 14.0}},
        },
        {
            "key": "slot_seam",
            "role": "Slot (playside)",
            "intent": "Seam release, read the safety leverage.",
            "coaching_point": "Vertical stem; bend away from the closing safety.",
            "intended_route": {
                "points": [[0.66, 0.85], [0.64, 0.45], [0.6, 0.16]],
                "color": "#15397F",
            },
            "rule": {
                "type": "release_direction",
                "params": {"axis": "y", "sign": -1, "min_delta": 0.12},
            },
        },
        {
            "key": "qb_read",
            "role": "QB",
            "intent": "Work the middle-of-field read; ball out on rhythm.",
            "coaching_point": "MOF-open vs MOF-closed determines the high-low.",
            "intended_route": None,
            "rule": {"type": "manual_only"},
        },
    ],
}

# Defensive structure: Cover 3 Sky. Three deep-third defenders (zone landmarks,
# which work on frame geometry without calibration) plus the strong-safety flat
# rotation graded by the coach.
COVER_3_SKY: dict[str, Any] = {
    "name": "Cover 3 Sky",
    "side": "defense",
    "structure_kind": "coverage",
    "description": (
        "Single-high three-deep, four-under zone with the strong safety rolling "
        "down to the flat (sky)."
    ),
    "coaching_points": [
        "Deep thirds: eyes on the QB, gain depth with the release.",
        "Curl-flat defenders sink to the curl, drive on the flat.",
    ],
    "assignments": [
        {
            "key": "deep_third_left",
            "role": "Corner (left third)",
            "intent": "Carry the deep outside third.",
            "coaching_point": "Bail to depth; never let anything cross your face deep.",
            "intended_route": {"region": {"x": [0.0, 0.33], "y": [0.0, 0.35]}, "color": "#15397F"},
            "rule": {
                "type": "zone_landmark",
                "params": {"region": {"x": [0.0, 0.33], "y": [0.0, 0.35]}},
            },
        },
        {
            "key": "deep_third_middle",
            "role": "Free safety (middle third)",
            "intent": "Carry the deep middle third.",
            "coaching_point": "Stay on top of the post; split the two deepest.",
            "intended_route": {"region": {"x": [0.33, 0.67], "y": [0.0, 0.35]}, "color": "#15397F"},
            "rule": {
                "type": "zone_landmark",
                "params": {"region": {"x": [0.33, 0.67], "y": [0.0, 0.35]}},
            },
        },
        {
            "key": "deep_third_right",
            "role": "Corner (right third)",
            "intent": "Carry the deep outside third.",
            "coaching_point": "Bail to depth; squeeze the vertical.",
            "intended_route": {"region": {"x": [0.67, 1.0], "y": [0.0, 0.35]}, "color": "#15397F"},
            "rule": {
                "type": "zone_landmark",
                "params": {"region": {"x": [0.67, 1.0], "y": [0.0, 0.35]}},
            },
        },
        {
            "key": "ss_flat",
            "role": "Strong safety (sky flat)",
            "intent": "Roll down to the strong flat.",
            "coaching_point": "Read #2 to #1; trigger on the flat threat.",
            "intended_route": None,
            "rule": {"type": "manual_only"},
        },
    ],
}

SEED_CONCEPTS: list[dict[str, Any]] = [FOUR_VERTICALS, COVER_3_SKY]
