"""Observed movement packets share the canonical scalar registry."""

from app.services.metric_definitions import DEFINITIONS


def test_movement_registry_uses_explicit_scalar_keys_and_evidence():
    expected = {
        "position_opportunity": "value",
        "observed_separation": "yards",
        "observed_rush_path": "yards",
        "observed_pursuit_path": "yards",
        "observed_closing_speed": "yards_per_second",
        "get_off_time": "seconds",
        "operation_time": "seconds",
        "hang_time": "seconds",
        "kick_landing_location": None,
        "te_usage_transition": "seconds",
    }
    for name, scalar_key in expected.items():
        assert DEFINITIONS[name]["scalar_key"] == scalar_key
        assert "qualified_position_participation" in DEFINITIONS[name]["required_evidence"]
    assert "negative means opening" in DEFINITIONS["observed_closing_speed"]["definition"]
    assert (
        "Coverage responsibility is not inferred"
        in DEFINITIONS["observed_separation"]["definition"]
    )


def test_specialist_geometry_definitions_keep_spatial_packets_out_of_scalar_aggregation():
    expected = {
        "protection_envelope_width": "width_yards",
        "protection_envelope_depth": "depth_yards",
        "protection_envelope_area": "area_square_yards",
        "protection_envelope_compression": "square_yards_per_second",
        "lineman_first_second_displacement": "yards",
        "observed_pass_set_geometry": "depth_yards",
        "observed_route_depth": "yards",
        "receiver_snap_alignment": "lateral_yards",
        "receiver_release_location": None,
        "receiver_break_location": None,
    }
    for name, scalar in expected.items():
        assert DEFINITIONS[name]["scalar_key"] == scalar
