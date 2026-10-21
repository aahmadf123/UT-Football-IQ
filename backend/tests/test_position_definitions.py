"""Position packets use the single central versioned scalar registry."""

from app.services.metric_definitions import DEFINITION_VERSION, metric_definitions


def test_position_producer_scalar_and_evidence_contracts():
    definitions = {d["name"]: d for d in metric_definitions()}
    assert definitions["clear_lane_width"]["scalar_key"] == "width_yards"
    assert definitions["signed_forward_gain"]["scalar_key"] == "yards"
    assert definitions["actual_entry_gap"]["scalar_key"] == "value"
    assert definitions["handoff_to_los"]["scalar_key"] == "seconds"
    assert (
        "continuous_qualified_pressure_coverage"
        in definitions["time_to_first_pressure"]["required_evidence"]
    )
    assert "complete_defender_occupancy" in definitions["clear_lane_width"]["required_evidence"]
    assert "not official" in definitions["signed_forward_gain"]["definition"]
    assert all(d["version"] == DEFINITION_VERSION for d in definitions.values())
