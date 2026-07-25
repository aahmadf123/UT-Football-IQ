"""Pydantic models for per-position-group alert threshold configuration.

Configurable by sports performance or coaching staff only
(require_sportsperformance_or_above or require_coach_or_above).

NOTE: Path deviation from ISSUE16_PLAN.md — the plan specified
backend/app/models/alert_config.py, but backend/app/models.py already exists
as a flat file (all ORM models live there).  This file lives in
backend/app/schemas/alert_config.py to avoid a Python import conflict.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# All known position groups
KNOWN_POSITION_GROUPS: frozenset[str] = frozenset(
    {"OL", "DL", "WR", "TE", "QB", "RB", "DB", "LB", "ST", "SKILL", "UNKNOWN"}
)

# ── Per-alert-type threshold models ───────────────────────────────────────────


class BioDeviationThresholds(BaseModel):
    """Biomechanical deviation alert thresholds.

    Alerts fire when a pose metric deviates >= deviation_sd standard
    deviations from the player's rolling 4-week baseline.  The pose pipeline
    must be active AND coach_approved must be True for this position group
    before any bio deviation alert is generated.
    """

    deviation_sd: float = Field(
        default=2.0,
        ge=1.0,
        le=5.0,
        description="SD threshold for biomechanical deviation alert",
    )
    min_baseline_samples: int = Field(
        default=5,
        ge=2,
        le=200,
        description="Minimum historical samples required to compute a baseline",
    )
    pose_pipeline_active: bool = Field(
        default=False,
        description="Whether Issue #6 pose pipeline is active for this position group",
    )
    coach_approved: bool = Field(
        default=False,
        description="Position coach has approved bio alerts for this group",
    )


class EffortAnomalyThresholds(BaseModel):
    """Effort anomaly (sprint-to-ball) alert thresholds."""

    deviation_sd: float = Field(
        default=1.5,
        ge=0.5,
        le=4.0,
        description="SDs below session mean to trigger an effort alert",
    )
    min_session_reps: int = Field(
        default=3,
        ge=2,
        le=50,
        description="Minimum session reps before anomaly detection runs",
    )


class FormationAnomalyThresholds(BaseModel):
    """Formation alignment anomaly alert thresholds."""

    threshold_yards: float = Field(
        default=3.0,
        ge=0.5,
        le=15.0,
        description="Yards off expected formation position to trigger alert",
    )


class WorkloadThresholds(BaseModel):
    """Workload-risk alert thresholds (Issue #149).

    Alerts fire from the nightly workload rollup when the acute:chronic
    workload ratio or the gait asymmetry index exceeds these values.  The
    resulting alerts are sports-performance-staff only — never shown to
    coaches, players, or viewers — and are experimental sports-performance
    indicators, not a diagnosis.
    """

    acwr_high: float = Field(
        default=1.5,
        ge=1.0,
        le=3.0,
        description="Acute:chronic workload ratio above which an alert fires",
    )
    asymmetry_high: float = Field(
        default=1.3,
        ge=1.0,
        le=2.0,
        description="Gait asymmetry index above which an alert fires",
    )
    min_chronic_days: int = Field(
        default=14,
        ge=7,
        le=28,
        description="Days of data required in the 28-day window before ACWR is reported",
    )


# ── Per-position-group config ──────────────────────────────────────────────────


class PositionGroupAlertConfig(BaseModel):
    """Alert configuration for a single position group."""

    position_group: str = Field(..., description="Position group identifier (e.g. OL, WR, QB)")
    bio_deviation: BioDeviationThresholds = Field(default_factory=BioDeviationThresholds)
    effort_anomaly: EffortAnomalyThresholds = Field(default_factory=EffortAnomalyThresholds)
    formation_anomaly: FormationAnomalyThresholds = Field(
        default_factory=FormationAnomalyThresholds
    )
    workload: WorkloadThresholds = Field(default_factory=WorkloadThresholds)


class AlertConfigRequest(BaseModel):
    """Request body for updating alert thresholds for a position group."""

    bio_deviation: BioDeviationThresholds | None = None
    effort_anomaly: EffortAnomalyThresholds | None = None
    formation_anomaly: FormationAnomalyThresholds | None = None
    workload: WorkloadThresholds | None = None


class AlertConfigResponse(BaseModel):
    """Response: full alert configuration for all position groups."""

    configs: dict[str, PositionGroupAlertConfig]


# ── Default factory ────────────────────────────────────────────────────────────


def default_alert_config() -> dict[str, PositionGroupAlertConfig]:
    """Return default alert thresholds for all known position groups."""
    return {pg: PositionGroupAlertConfig(position_group=pg) for pg in KNOWN_POSITION_GROUPS}
