"""Athlete health/workload **surface** groundwork (Issue #113).

This module is deliberately distinct from :mod:`app.workload`, which gates
*heavy backend jobs* on GPU-queue capacity.  Here we model the **athlete**
health/workload product surface — wellness, GPS/wearables, and strength &
conditioning context — that sits behind the ``health_workload`` RBAC policy.

No sensitive athlete data is surfaced yet.  Until the integration contracts
below are wired to real providers, the surface only describes:

* which **integration sources** are planned and their connection status, and
* the **policy guardrails** the UI must honour (non-medical, no injury
  diagnosis or prediction).

Keeping the contract in code (rather than only in prose) lets the backend,
frontend, and docs share one definition, and lets tests assert that the
surface never leaks athlete PII.

The single source of truth for *who* may read this surface is
``app.governance.POLICY[(Resource.HEALTH_WORKLOAD, Action.READ)]`` —
sports-performance staff plus analytics leads and admins.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from app.models import UserRole

# ── Policy-safe copy ─────────────────────────────────────────────────────────

# Shown verbatim by the UI.  Reviewed so the surface never implies a medical
# device, a diagnosis, or an injury prediction.  The workload-risk carve-out
# (Issue #149) is deliberate: experimental CV workload indicators may be shown
# to sports-performance staff, but they remain sports-performance context —
# they never become a diagnosis or a medical prediction.
SURFACE_DISCLAIMER: str = (
    "Training-load and wellness context for sports-performance staff only. "
    "This surface is not a medical device and does not diagnose injuries or "
    "predict injury risk. Experimental workload-risk indicators are "
    "sports-performance signals for staff review — not a diagnosis. "
    "It supports staff judgement; it does not replace it."
)

# Per-row caveat attached to every workload-risk value the API returns.
WORKLOAD_RISK_CAVEAT: str = (
    "Experimental sports-performance indicator — not a diagnosis and not a "
    "medical prediction of injury."
)

# Identity-confidence floor for player-attributed workload rows. Mirrors the
# gpu-worker's LOW_IDENTITY_CONFIDENCE gate (pipeline/metrics/effort_zscore.py):
# below this, workload stays on the anonymous track and never becomes a
# named-player row — enforced again here as defense in depth on ingest.
MIN_IDENTITY_CONFIDENCE: float = 0.70

# In-app policy statement (Issue #9) — shown on every fused health/workload
# dashboard view, verbatim.
POLICY_STATEMENT: str = (
    "CV outputs are workload/movement context that support staff judgment — "
    "they are not a medical diagnosis, and no value on this surface is ground "
    "truth. Staff decisions always take precedence."
)


# ── Integration contracts ────────────────────────────────────────────────────


class IntegrationSource(enum.StrEnum):
    """Planned upstream sources for the athlete health/workload surface."""

    wellness = "wellness"
    gps_wearables = "gps_wearables"
    strength_conditioning = "strength_conditioning"
    academic_calendar = "academic_calendar"
    injury_history = "injury_history"


class IntegrationStatus(enum.StrEnum):
    """Connection state of an :class:`IntegrationSource`.

    Only ``not_connected`` is used today — this surface is groundwork and no
    provider is wired yet.  ``connected`` is reserved for when a real feed
    lands so callers can branch without a schema change.
    """

    not_connected = "not_connected"
    connected = "connected"


@dataclass(frozen=True, slots=True)
class IntegrationContract:
    """A placeholder contract for one upstream health/workload source.

    Describes *what the surface will accept* once wired, deliberately at the
    category level — never field-level athlete data.
    """

    source: IntegrationSource
    display_name: str
    description: str
    status: IntegrationStatus
    # Coarse, non-diagnostic categories the source would provide.
    data_categories: tuple[str, ...]
    # Generic, vendor-neutral examples of where the data originates.
    provider_examples: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "display_name": self.display_name,
            "description": self.description,
            "status": self.status.value,
            "data_categories": list(self.data_categories),
            "provider_examples": list(self.provider_examples),
        }


# The three placeholder contracts called out by Issue #113.  All start
# ``not_connected``; flipping one to ``connected`` is a future, separately
# reviewed change that must also pass the policy/audit gates here.
INTEGRATION_CONTRACTS: tuple[IntegrationContract, ...] = (
    IntegrationContract(
        source=IntegrationSource.wellness,
        display_name="Wellness self-report",
        description=(
            "Athlete-submitted daily wellness check-ins. Self-reported context "
            "only — never a clinical assessment."
        ),
        status=IntegrationStatus.not_connected,
        data_categories=(
            "self_reported_soreness",
            "self_reported_sleep",
            "self_reported_energy",
        ),
        provider_examples=(
            "Team wellness questionnaire",
            "Athlete check-in app",
        ),
    ),
    IntegrationContract(
        source=IntegrationSource.gps_wearables,
        display_name="GPS / wearables",
        description=(
            "Practice and game movement load from GPS units and wearables — "
            "distance, speed bands, and accelerations as training-load context."
        ),
        status=IntegrationStatus.not_connected,
        data_categories=(
            "total_distance",
            "high_speed_distance",
            "accelerations",
            "player_load",
        ),
        provider_examples=(
            "GPS tracking vest",
            "Wrist / chest wearable",
        ),
    ),
    IntegrationContract(
        source=IntegrationSource.strength_conditioning,
        display_name="Strength & conditioning",
        description=(
            "Weight-room and conditioning volume logged by S&C staff — session "
            "load and tonnage as planning context."
        ),
        status=IntegrationStatus.not_connected,
        data_categories=(
            "session_volume",
            "tonnage",
            "session_rpe",
        ),
        provider_examples=(
            "S&C session log",
            "Weight-room tracking sheet",
        ),
    ),
    IntegrationContract(
        source=IntegrationSource.academic_calendar,
        display_name="Academic calendar",
        description=(
            "Team-level academic-stress overlay (exams, midterms, breaks, "
            "travel). Calendar context only — no per-player academic data."
        ),
        status=IntegrationStatus.not_connected,
        data_categories=(
            "exam_periods",
            "breaks",
            "travel_windows",
        ),
        provider_examples=(
            "University academic calendar",
            "Team travel schedule",
        ),
    ),
    IntegrationContract(
        source=IntegrationSource.injury_history,
        display_name="Injury history",
        description=(
            "Coarse prior-injury context (body region, side, days missed) — "
            "rehab-tier restricted; no diagnosis text is ever stored."
        ),
        status=IntegrationStatus.not_connected,
        data_categories=(
            "body_region",
            "coarse_status",
            "days_missed",
        ),
        provider_examples=("Athletic-training availability log",),
    ),
)


# Roles approved to read the surface.  Derived from the central RBAC policy so
# this list cannot drift from what the dependency actually enforces.
def approved_roles() -> list[str]:
    # Imported lazily to avoid a circular import: governance imports models,
    # which this module also imports.
    from app.governance import POLICY, Action, Resource  # noqa: PLC0415

    allowed = POLICY.get((Resource.HEALTH_WORKLOAD, Action.READ), frozenset())
    return sorted(role.value for role in allowed)


def build_surface_status(
    *, role: UserRole, source_counts: dict[str, int] | None = None
) -> dict[str, Any]:
    """Return the policy-safe surface status for an authorized caller.

    The payload intentionally contains **no athlete data and no PII** — only
    the viewer's role, the integration contracts, the approved-role list, and
    the disclaimer.  ``source_counts`` (source value → persisted row count)
    lets callers with DB access flip a contract to ``connected`` when real
    rows exist; without it every contract reports ``not_connected`` and
    ``data_available`` stays ``False``.
    """
    counts = source_counts or {}
    integrations = []
    any_connected = False
    for contract in INTEGRATION_CONTRACTS:
        payload = contract.to_dict()
        if counts.get(contract.source.value, 0) > 0:
            payload["status"] = IntegrationStatus.connected.value
            any_connected = True
        integrations.append(payload)
    return {
        "role": role.value,
        "data_available": any_connected,
        "disclaimer": SURFACE_DISCLAIMER,
        "approved_roles": approved_roles(),
        "integrations": integrations,
    }
