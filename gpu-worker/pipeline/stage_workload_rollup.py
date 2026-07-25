"""Nightly per-player workload rollup stage (Issue #149).

Dispatched by the Cloudflare cron trigger (``workers/wrangler.toml``
``[triggers]``) as a ``workload_rollup`` job on the nightly queue. For the
target day it:

1. Fetches per-player daily CV loads from the backend for the trailing
   28-day window (recomputed from persisted ``workload_fusion`` metric rows
   each night — idempotent and self-healing).
2. Computes the acute:chronic workload ratio and the deterministic
   ``acwr-asym-heuristic-v1`` injury-risk score per player
   (``pipeline.metrics.workload_fusion``), stamping the model-router variant
   for the #73 audit trail.
3. Upserts one ``player_workload_daily`` row per player who has data on the
   target day (identity-confident, player-attributed rows only — the
   backend re-checks the confidence floor on ingest).
4. Fires ``workload_risk`` alerts when ACWR > 1.5 or asymmetry > 1.3
   (sports-performance staff only; never coaches/players/viewers).

All outputs are experimental sports-performance indicators — never a
diagnosis.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from alerts import workload_risk_alert
from pipeline import backend, model_router
from pipeline.metrics.effort_zscore import LOW_IDENTITY_CONFIDENCE
from pipeline.metrics.workload_fusion import compute_acwr, compute_injury_risk

log = structlog.get_logger(__name__)

# Trailing window the chronic load is computed over.
CHRONIC_WINDOW_DAYS = 28


def run(date: str, job_id: str, *, priority: int = 0) -> dict[str, Any]:
    """Run the nightly workload rollup for ``date`` (ISO ``YYYY-MM-DD``)."""
    as_of = dt.date.fromisoformat(date[:10])
    variant = model_router.select_model("workload_fusion", priority)
    log.info("stage_workload_rollup_start", date=as_of.isoformat(), model_variant=variant)

    # ── 1. Trailing window of per-player daily loads ─────────────────────────
    daily_loads_by_player: dict[str, dict[dt.date, float]] = {}
    today_by_player: dict[str, dict[str, Any]] = {}

    for offset in range(CHRONIC_WINDOW_DAYS):
        day = as_of - dt.timedelta(days=offset)
        try:
            entries = backend.fetch_daily_cv_loads(day.isoformat())
        except Exception as exc:
            log.warning(
                "workload_rollup_day_fetch_failed", day=day.isoformat(), error=str(exc)
            )
            continue
        for entry in entries:
            player_id = str(entry.get("player_id") or "")
            if not player_id:
                continue
            load = entry.get("daily_load")
            if load is not None:
                daily_loads_by_player.setdefault(player_id, {})[day] = float(load)
            if day == as_of:
                today_by_player[player_id] = entry

    # ── 2 + 3. Compute and upsert rows for identity-confident players ──────────
    rows: list[dict[str, Any]] = []
    for player_id, today in today_by_player.items():
        # Skip rows that lack sufficient identity confidence — the backend
        # re-checks at ingest, but we filter early to avoid posting rows that
        # would be rejected, and to ensure alerts are only fired for rows that
        # will actually be persisted.
        conf = today.get("confidence")
        if conf is None or float(conf) < LOW_IDENTITY_CONFIDENCE:
            log.debug(
                "workload_rollup_skipped_low_confidence",
                player_id=player_id,
                confidence=conf,
            )
            continue
        acwr_result = compute_acwr(daily_loads_by_player.get(player_id, {}), as_of)
        asymmetry = today.get("asymmetry_index")
        risk = compute_injury_risk(acwr_result["acwr"], asymmetry)
        reason_codes = sorted(set(acwr_result["reason_codes"]) | set(risk["reason_codes"]))
        rows.append(
            {
                "player_id": player_id,
                "date": as_of.isoformat(),
                "daily_load": today.get("daily_load"),
                "sprint_count": today.get("sprint_count"),
                "max_speed_yps": today.get("max_speed_yps"),
                "asymmetry_index": asymmetry,
                "acute_load_7d": acwr_result["acute_load_7d"],
                "chronic_load_28d": acwr_result["chronic_load_28d"],
                "acwr": acwr_result["acwr"],
                "injury_risk_score": risk["injury_risk_score"],
                "risk_reason_codes": reason_codes,
                "clip_count": today.get("clip_count") or 0,
                "attribution": "player",
                "confidence": conf,
                "model_variant": variant,
                # Not persisted — used by the alert builder below.
                "position_group": today.get("position_group"),
            }
        )

    upserted = 0
    upsert_ok = False
    if rows:
        try:
            upserted = backend.upsert_workload_daily(
                [{k: v for k, v in row.items() if k != "position_group"} for row in rows]
            )
            upsert_ok = True
        except Exception as exc:
            log.warning("workload_rollup_upsert_failed", error=str(exc))

    # ── 4. Threshold alerts (sports-performance only) ────────────────────────
    # Only fire alerts when the upsert succeeded — alerts must only reference
    # rows that were actually persisted.
    alerts_created = 0
    if upsert_ok and rows:
        alert_payloads = workload_risk_alert.run(rows)
        alerts_created = backend.create_alerts(alert_payloads) if alert_payloads else 0

    log.info(
        "stage_workload_rollup_done",
        date=as_of.isoformat(),
        players=len(rows),
        upserted=upserted,
        alerts=alerts_created,
    )
    return {
        "date": as_of.isoformat(),
        "players": len(rows),
        "upserted": upserted,
        "alerts_created": alerts_created,
        "model_variant": variant,
    }
