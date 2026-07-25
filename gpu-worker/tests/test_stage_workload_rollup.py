"""Tests for the nightly workload rollup stage (Issue #149)."""

import datetime as dt
import os
import sys
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import stage_workload_rollup

AS_OF = dt.date(2026, 7, 1)


def _entry(player_id: str, load: float, **overrides: Any) -> dict[str, Any]:
    entry = {
        "player_id": player_id,
        "position_group": "WR",
        "daily_load": load,
        "sprint_count": 3,
        "max_speed_yps": 7.5,
        "asymmetry_index": 1.1,
        "confidence": 0.85,
        "clip_count": 4,
    }
    entry.update(overrides)
    return entry


def _loads_by_day(
    steady: float = 100.0,
    spike_recent: float | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """28 days of history: p1 steady; p2 optionally spiking in the last 7 days."""
    days: dict[str, list[dict[str, Any]]] = {}
    for offset in range(28):
        day = (AS_OF - dt.timedelta(days=offset)).isoformat()
        entries = [_entry("p1", steady)]
        if spike_recent is not None:
            p2_load = spike_recent if offset < 7 else steady
            p2 = _entry("p2", p2_load, asymmetry_index=1.4, sprint_count=8)
            entries.append(p2)
        days[day] = entries
    return days


def _run(loads_by_day: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    calls: dict[str, Any] = {"upserted_rows": None, "alerts": None}

    def _fetch(date: str) -> list[dict[str, Any]]:
        return loads_by_day.get(date, [])

    def _upsert(rows: list[dict[str, Any]]) -> int:
        calls["upserted_rows"] = rows
        return len(rows)

    def _create_alerts(payloads: list[dict[str, Any]]) -> int:
        calls["alerts"] = payloads
        return len(payloads)

    with (
        patch.object(stage_workload_rollup.backend, "fetch_daily_cv_loads", side_effect=_fetch),
        patch.object(stage_workload_rollup.backend, "upsert_workload_daily", side_effect=_upsert),
        patch.object(stage_workload_rollup.backend, "create_alerts", side_effect=_create_alerts),
    ):
        result = stage_workload_rollup.run(AS_OF.isoformat(), job_id="job-1")
    return result, calls


def test_steady_load_rolls_up_with_acwr_one_and_no_alert() -> None:
    result, calls = _run(_loads_by_day())

    assert result["players"] == 1
    assert result["upserted"] == 1
    assert result["alerts_created"] == 0
    row = calls["upserted_rows"][0]
    assert row["player_id"] == "p1"
    assert row["acwr"] == pytest.approx(1.0)
    assert row["acute_load_7d"] == pytest.approx(100.0)
    assert row["chronic_load_28d"] == pytest.approx(100.0)
    # The stage stamps the routed model variant on every row (#73).
    assert row["model_variant"] == "acwr-asym-heuristic-v1"
    # position_group feeds the alert builder but is never persisted.
    assert "position_group" not in row


def test_acute_spike_fires_workload_risk_alert() -> None:
    # p2: 7 days at 250 after 21 days at 100 → acwr ≈ 1.82 > 1.5, plus
    # asymmetry 1.4 > 1.3 → high-severity alert.
    result, calls = _run(_loads_by_day(spike_recent=250.0))

    assert result["players"] == 2
    assert result["alerts_created"] == 1
    alert = calls["alerts"][0]
    assert alert["alert_type"] == "workload_risk"
    assert alert["player_id"] == "p2"
    assert alert["severity"] == "high"
    assert alert["position_group"] == "WR"

    p2_row = next(r for r in calls["upserted_rows"] if r["player_id"] == "p2")
    assert p2_row["acwr"] == pytest.approx(1.818, abs=0.01)
    assert "acwr_above_threshold" in p2_row["risk_reason_codes"]
    assert "asymmetry_above_threshold" in p2_row["risk_reason_codes"]


def test_insufficient_history_reports_null_acwr() -> None:
    # Data on only 5 of 28 days → below MIN_CHRONIC_DAYS → acwr None.
    days = {
        (AS_OF - dt.timedelta(days=offset)).isoformat(): [_entry("p1", 100.0)]
        for offset in range(5)
    }
    result, calls = _run(days)

    assert result["players"] == 1
    row = calls["upserted_rows"][0]
    assert row["acwr"] is None
    assert "insufficient_chronic_history" in row["risk_reason_codes"]


def test_no_data_today_means_no_rows() -> None:
    # History exists but nothing on the target day → nothing to upsert.
    days = {
        (AS_OF - dt.timedelta(days=offset)).isoformat(): [_entry("p1", 100.0)]
        for offset in range(1, 28)
    }
    result, calls = _run(days)

    assert result["players"] == 0
    assert result["upserted"] == 0
    assert calls["upserted_rows"] is None


def test_low_identity_players_are_skipped_entirely() -> None:
    # A spiking player with sub-floor identity confidence must produce no
    # rollup row and no alert — the anonymous track never becomes a named row.
    days = {
        (AS_OF - dt.timedelta(days=offset)).isoformat(): [
            _entry("p1", 100.0),
            _entry(
                "shadow",
                300.0 if offset < 7 else 100.0,
                confidence=0.4,
                asymmetry_index=1.6,
            ),
        ]
        for offset in range(28)
    }
    result, calls = _run(days)

    assert result["players"] == 1
    assert result["alerts_created"] == 0
    assert [r["player_id"] for r in calls["upserted_rows"]] == ["p1"]


def test_upsert_failure_suppresses_alerts() -> None:
    # An alert must never exist without its persisted rollup row.
    loads = _loads_by_day(spike_recent=250.0)
    alerts_seen: list[Any] = []

    def _failing_upsert(rows: list[dict[str, Any]]) -> int:
        raise RuntimeError("backend down")

    with (
        patch.object(
            stage_workload_rollup.backend,
            "fetch_daily_cv_loads",
            side_effect=lambda date: loads.get(date, []),
        ),
        patch.object(
            stage_workload_rollup.backend, "upsert_workload_daily", side_effect=_failing_upsert
        ),
        patch.object(
            stage_workload_rollup.backend,
            "create_alerts",
            side_effect=lambda p: alerts_seen.extend(p) or len(p),
        ),
    ):
        result = stage_workload_rollup.run(AS_OF.isoformat(), job_id="job-1")

    assert result["upserted"] == 0
    assert result["alerts_created"] == 0
    assert alerts_seen == []


def test_fetch_failure_for_one_day_does_not_abort() -> None:
    loads = _loads_by_day()
    calls_seen: list[str] = []

    def _flaky_fetch(date: str) -> list[dict[str, Any]]:
        calls_seen.append(date)
        if date == (AS_OF - dt.timedelta(days=3)).isoformat():
            raise RuntimeError("backend hiccup")
        return loads.get(date, [])

    with (
        patch.object(
            stage_workload_rollup.backend, "fetch_daily_cv_loads", side_effect=_flaky_fetch
        ),
        patch.object(
            stage_workload_rollup.backend, "upsert_workload_daily", side_effect=lambda rows: len(rows)
        ),
        patch.object(stage_workload_rollup.backend, "create_alerts", side_effect=lambda p: len(p)),
    ):
        result = stage_workload_rollup.run(AS_OF.isoformat(), job_id="job-1")

    assert len(calls_seen) == 28
    assert result["players"] == 1
