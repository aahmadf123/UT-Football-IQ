"""Settings router (Issue #112).

Backs the Football-IQ settings UI with real persistence.  System-level
config and the model-sensitivity dials are admin-only; per-user preferences
are read/written by the authenticated user only.

Endpoints:
    GET   /api/v1/settings/system   — full system settings (any auth user can read)
    PATCH /api/v1/settings/system   — admin-only update
    GET   /api/v1/settings/me       — current user's preferences
    PATCH /api/v1/settings/me       — update current user's preferences

TODO(governance): once the governance framework expands beyond player
visibility, replace the role-based guards here with ``require_policy`` checks
against a dedicated ``SETTINGS`` resource.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, require_admin
from app.models import SystemSetting, User, UserSetting
from app.schemas.settings import (
    MODEL_SENSITIVITY_KEY,
    SYSTEM_CONFIG_KEY,
    USER_PREFERENCES_KEY,
    ModelSensitivity,
    SystemConfig,
    SystemSettingsResponse,
    SystemSettingsUpdate,
    UserPreferences,
    UserPreferencesUpdate,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _load_system_setting(db: AsyncSession, key: str) -> dict[str, Any] | None:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    return dict(row.value) if row is not None else None


async def _upsert_system_setting(
    db: AsyncSession,
    *,
    key: str,
    value: dict[str, Any],
    actor_id: Any,
) -> None:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        db.add(SystemSetting(key=key, value=value, updated_by=actor_id))
    else:
        row.value = value
        row.updated_by = actor_id
    await db.flush()


async def _load_user_setting(db: AsyncSession, user_id: Any, key: str) -> dict[str, Any] | None:
    result = await db.execute(
        select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key)
    )
    row = result.scalar_one_or_none()
    return dict(row.value) if row is not None else None


async def _upsert_user_setting(
    db: AsyncSession, *, user_id: Any, key: str, value: dict[str, Any]
) -> None:
    result = await db.execute(
        select(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key)
    )
    row = result.scalar_one_or_none()
    if row is None:
        db.add(UserSetting(user_id=user_id, key=key, value=value))
    else:
        row.value = value
    await db.flush()


async def _load_system_response(db: AsyncSession) -> SystemSettingsResponse:
    sys_raw = await _load_system_setting(db, SYSTEM_CONFIG_KEY)
    sens_raw = await _load_system_setting(db, MODEL_SENSITIVITY_KEY)
    return SystemSettingsResponse(
        system_config=SystemConfig(**(sys_raw or {})),
        model_sensitivity=ModelSensitivity(**(sens_raw or {})),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/system", response_model=SystemSettingsResponse)
async def get_system_settings(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    _current_user: Annotated[User, Depends(get_current_user)],
) -> SystemSettingsResponse:
    """Return full system settings (config + model sensitivity).

    Values are not sensitive; any authenticated user may read them so the UI
    can render them in a read-only state for non-admin roles.
    """
    return await _load_system_response(db)


@router.patch("/system", response_model=SystemSettingsResponse)
async def update_system_settings(
    body: SystemSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(require_admin)],
) -> SystemSettingsResponse:
    """Admin-only partial update of system settings.

    Only provided sections (``system_config``, ``model_sensitivity``) are
    touched.  Within each section, only provided fields are merged onto the
    existing persisted blob (or onto the schema defaults if no row exists).
    """
    if body.system_config is not None:
        current_raw = await _load_system_setting(db, SYSTEM_CONFIG_KEY)
        merged = SystemConfig(**(current_raw or {})).model_dump()
        updates = body.system_config.model_dump(exclude_unset=True)
        merged.update(updates)
        # Re-validate the merged shape so out-of-range values never persist.
        validated = SystemConfig(**merged).model_dump()
        await _upsert_system_setting(
            db, key=SYSTEM_CONFIG_KEY, value=validated, actor_id=current_user.id
        )

    if body.model_sensitivity is not None:
        current_raw = await _load_system_setting(db, MODEL_SENSITIVITY_KEY)
        merged = ModelSensitivity(**(current_raw or {})).model_dump()
        updates = body.model_sensitivity.model_dump(exclude_unset=True)
        merged.update(updates)
        validated = ModelSensitivity(**merged).model_dump()
        await _upsert_system_setting(
            db, key=MODEL_SENSITIVITY_KEY, value=validated, actor_id=current_user.id
        )

    log.info("system_settings_updated", actor=str(current_user.id))
    return await _load_system_response(db)


@router.get("/me", response_model=UserPreferences)
async def get_my_preferences(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserPreferences:
    raw = await _load_user_setting(db, current_user.id, USER_PREFERENCES_KEY)
    return UserPreferences(**(raw or {}))


@router.patch("/me", response_model=UserPreferences)
async def update_my_preferences(
    body: UserPreferencesUpdate,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserPreferences:
    current_raw = await _load_user_setting(db, current_user.id, USER_PREFERENCES_KEY)
    merged = UserPreferences(**(current_raw or {})).model_dump()
    updates = body.model_dump(exclude_unset=True)
    merged.update(updates)
    validated = UserPreferences(**merged)
    await _upsert_user_setting(
        db, user_id=current_user.id, key=USER_PREFERENCES_KEY, value=validated.model_dump()
    )
    log.info("user_preferences_updated", user=str(current_user.id))
    return validated
