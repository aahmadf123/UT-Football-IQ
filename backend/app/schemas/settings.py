"""Pydantic schemas for system + user settings (Issue #112).

The persisted rows in ``system_settings`` and ``user_settings`` are flexible
JSON blobs; these schemas pin down the known-key shapes so the API never
returns ``null`` for a field and the frontend has a stable contract.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Persistence keys — used as the ``key`` column value in ``system_settings``
# and ``user_settings``. Centralised so router + tests stay in sync.
SYSTEM_CONFIG_KEY = "system_config"
MODEL_SENSITIVITY_KEY = "model_sensitivity"
USER_PREFERENCES_KEY = "user_preferences"


class SystemConfig(BaseModel):
    """Coaching-staff-facing system configuration.

    The current ``SettingsView`` exposes four System Config fields
    (team name, capture camera, storage bucket, auto-export access). Only an
    admin may update these values.

    ``storage_bucket`` is **display-only / informational**: the actual R2
    bucket bindings are env-driven and cannot be hot-swapped at runtime. The
    PATCH endpoint accepts writes so admins can record intent, but the value
    is not consumed by the file-IO path.
    """

    team_name: str = Field(default="Toledo Rockets", max_length=120)
    capture_camera: str = Field(default="Drone (primary)", max_length=120)
    storage_bucket: str = Field(default="artifacts", max_length=120)
    auto_export_access: Literal["off", "staff", "all"] = "staff"
    # Set-and-forget: registering a video also creates its pipeline job, so
    # film never sits idle. The Film Room "Process Film" CTA remains for
    # manual mode (toggle off) and for retries.
    auto_process_on_upload: bool = True


class SystemConfigUpdate(BaseModel):
    """PATCH variant — every field optional, only provided keys are updated."""

    model_config = ConfigDict(extra="forbid")

    team_name: str | None = Field(default=None, max_length=120)
    capture_camera: str | None = Field(default=None, max_length=120)
    storage_bucket: str | None = Field(default=None, max_length=120)
    auto_export_access: Literal["off", "staff", "all"] | None = None
    auto_process_on_upload: bool | None = None


class ModelSensitivity(BaseModel):
    """Model sensitivity dials surfaced on the Settings page.

    Values are 0–100 integers (the UI renders them as percentages); the
    pipeline can map them onto its own thresholds without the frontend caring
    about the underlying units.
    """

    boundary_sensitivity: int = Field(default=68, ge=0, le=100)
    identity_confidence: int = Field(default=82, ge=0, le=100)
    motion_minimum: int = Field(default=72, ge=0, le=100)
    pose_review_gate: int = Field(default=88, ge=0, le=100)


class ModelSensitivityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boundary_sensitivity: int | None = Field(default=None, ge=0, le=100)
    identity_confidence: int | None = Field(default=None, ge=0, le=100)
    motion_minimum: int | None = Field(default=None, ge=0, le=100)
    pose_review_gate: int | None = Field(default=None, ge=0, le=100)


class SystemSettingsResponse(BaseModel):
    """Full system-settings payload returned by GET /api/v1/settings/system."""

    model_config = ConfigDict(protected_namespaces=())

    system_config: SystemConfig
    model_sensitivity: ModelSensitivity


class SystemSettingsUpdate(BaseModel):
    """PATCH /api/v1/settings/system body."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    system_config: SystemConfigUpdate | None = None
    model_sensitivity: ModelSensitivityUpdate | None = None


class UserPreferences(BaseModel):
    """Per-user preferences. Today these mirror the filter state currently
    persisted to ``localStorage``; backing them with the DB lets prefs follow
    the user across devices.
    """

    theme: Literal["light", "dark", "system"] = "system"
    default_session_kind: Literal["all", "practice", "scrimmage", "game"] = "all"
    default_side_of_ball: Literal["all", "offense", "defense", "special"] = "all"


class UserPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: Literal["light", "dark", "system"] | None = None
    default_session_kind: Literal["all", "practice", "scrimmage", "game"] | None = None
    default_side_of_ball: Literal["all", "offense", "defense", "special"] | None = None
