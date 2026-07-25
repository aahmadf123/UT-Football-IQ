"""Workload-risk alert builder (Issue #149).

Fires from the nightly per-player workload rollup when the acute:chronic
workload ratio exceeds ``acwr_high`` (default 1.5) or the gait asymmetry
index exceeds ``asymmetry_high`` (default 1.3).

Alert governance (non-negotiable):
  - Workload-risk alerts are sports-performance-staff only. The backend
    restricts the ``workload_risk`` alert type to admin + sportsperformance
    across list/get/SSE — they are never shown to coaches, players, or
    viewers.
  - Only identity-confident, player-attributed rollup rows may alert;
    anonymous-track rows never produce a named-player alert.
  - Every alert is an experimental sports-performance indicator — never a
    diagnosis. The payload carries reason codes and the model-router variant
    so staff can see exactly why it fired.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)

DEFAULT_ACWR_HIGH: float = 1.5
DEFAULT_ASYMMETRY_HIGH: float = 1.3


def run(
    rollup_rows: list[dict[str, Any]],
    *,
    acwr_high: float = DEFAULT_ACWR_HIGH,
    asymmetry_high: float = DEFAULT_ASYMMETRY_HIGH,
) -> list[dict[str, Any]]:
    """Build workload-risk alert payloads from nightly rollup rows.

    ``rollup_rows`` are the dicts upserted into ``player_workload_daily``
    (player_id, date, acwr, asymmetry_index, injury_risk_score,
    risk_reason_codes, position_group, confidence, model_variant, ...).

    Returns AlertCreate-shaped dicts; empty list when nothing exceeds the
    thresholds.
    """
    alerts: list[dict[str, Any]] = []
    timestamp = datetime.now(UTC).isoformat()

    for row in rollup_rows:
        player_id = row.get("player_id")
        if not player_id or row.get("attribution") != "player":
            # Anonymous/low-confidence rows never become named-player alerts.
            continue

        acwr = _number(row.get("acwr"))
        asymmetry = _number(row.get("asymmetry_index"))
        acwr_exceeded = acwr is not None and acwr > acwr_high
        asymmetry_exceeded = asymmetry is not None and asymmetry > asymmetry_high
        if not acwr_exceeded and not asymmetry_exceeded:
            continue

        severity = "high" if (acwr_exceeded and asymmetry_exceeded) else "medium"
        reason_codes = list(row.get("risk_reason_codes") or [])
        log.info(
            "workload_risk_alert",
            player_id=str(player_id),
            severity=severity,
            acwr_exceeded=acwr_exceeded,
            asymmetry_exceeded=asymmetry_exceeded,
        )
        alerts.append(
            {
                "alert_type": "workload_risk",
                "player_id": str(player_id),
                "position_group": str(row.get("position_group") or "UNKNOWN"),
                "severity": severity,
                "confidence": round(float(row.get("confidence") or 0.0), 3),
                "metric_name": "workload_fusion",
                "metric_value": {
                    "date": row.get("date"),
                    "acwr": acwr,
                    "acwr_threshold": acwr_high,
                    "asymmetry_index": asymmetry,
                    "asymmetry_threshold": asymmetry_high,
                    "injury_risk_score": _number(row.get("injury_risk_score")),
                    "reason_codes": reason_codes,
                    "model_variant": row.get("model_variant"),
                    "caveat": (
                        "Experimental sports-performance indicator — not a "
                        "diagnosis and not a medical prediction of injury."
                    ),
                },
                "session_id": f"{row.get('date')}-workload-rollup",
                "timestamp": timestamp,
            }
        )

    return alerts


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
