"""Tests for the workload-risk alert builder (Issue #149)."""

import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alerts.workload_risk_alert import run


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "player_id": "player-1",
        "date": "2026-07-01",
        "position_group": "WR",
        "attribution": "player",
        "acwr": 1.0,
        "asymmetry_index": 1.0,
        "injury_risk_score": 0.1,
        "risk_reason_codes": [],
        "confidence": 0.85,
        "model_variant": "acwr-asym-heuristic-v1",
    }
    row.update(overrides)
    return row


def test_no_alert_inside_thresholds() -> None:
    assert run([_row(acwr=1.5, asymmetry_index=1.3)]) == []  # thresholds are strict


def test_acwr_breach_fires_medium_alert() -> None:
    alerts = run([_row(acwr=1.8)])

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["alert_type"] == "workload_risk"
    assert alert["severity"] == "medium"
    assert alert["metric_value"]["acwr"] == 1.8
    assert "not a diagnosis" in alert["metric_value"]["caveat"]


def test_asymmetry_breach_fires_medium_alert() -> None:
    alerts = run([_row(asymmetry_index=1.45)])

    assert len(alerts) == 1
    assert alerts[0]["severity"] == "medium"


def test_double_breach_fires_high_alert() -> None:
    alerts = run([_row(acwr=1.9, asymmetry_index=1.5)])

    assert len(alerts) == 1
    assert alerts[0]["severity"] == "high"


def test_anonymous_rows_never_alert() -> None:
    rows = [
        _row(acwr=2.5, attribution="anonymous_track"),
        _row(acwr=2.5, player_id=None),
    ]
    assert run(rows) == []


def test_missing_signals_never_alert() -> None:
    assert run([_row(acwr=None, asymmetry_index=None)]) == []


def test_custom_thresholds_respected() -> None:
    alerts = run([_row(acwr=1.3)], acwr_high=1.2)

    assert len(alerts) == 1
    assert alerts[0]["metric_value"]["acwr_threshold"] == 1.2


def test_alert_payload_carries_model_variant_and_reasons() -> None:
    alerts = run([_row(acwr=1.8, risk_reason_codes=["acwr_above_threshold"])])

    value = alerts[0]["metric_value"]
    assert value["model_variant"] == "acwr-asym-heuristic-v1"
    assert value["reason_codes"] == ["acwr_above_threshold"]
    assert alerts[0]["metric_name"] == "workload_fusion"
