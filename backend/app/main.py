"""Football-IQ backend — FastAPI application entrypoint."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from app.config import get_settings
from app.logging import configure_logging
from app.observability import PrometheusMiddleware, metrics_response
from app.routers import health
from app.routers.alerts import router as alerts_router
from app.routers.alerts_sse import router as alerts_sse_router
from app.routers.auth import router as auth_router
from app.routers.calibrations import router as calibrations_router
from app.routers.cfbd import router as cfbd_router
from app.routers.clips import router as clips_router
from app.routers.concept_proposals import router as concept_proposals_router
from app.routers.concept_search import router as concept_search_router
from app.routers.correction_analytics import (
    router as correction_analytics_router,
)
from app.routers.correction_sync import router as correction_sync_router
from app.routers.corrections import router as corrections_router
from app.routers.counterfactuals import router as counterfactuals_router
from app.routers.embeddings import router as embeddings_router
from app.routers.events import router as events_router
from app.routers.frontier_analytics import router as frontier_analytics_router
from app.routers.health_ingest import router as health_ingest_router
from app.routers.health_workload import router as health_workload_router
from app.routers.inbox_integration import router as inbox_router
from app.routers.jobs import router as jobs_router
from app.routers.labels import router as labels_router
from app.routers.metrics import router as metrics_router
from app.routers.mlops import router as mlops_router
from app.routers.opponents import router as opponents_router
from app.routers.overlays import router as overlays_router
from app.routers.play_prediction import router as play_prediction_router
from app.routers.playbook import router as playbook_router
from app.routers.player_profiles import router as player_profiles_router
from app.routers.players import router as players_router
from app.routers.pose import router as pose_router
from app.routers.practice_sessions import router as practice_sessions_router
from app.routers.reports import router as reports_router
from app.routers.search import router as search_router
from app.routers.self_scout import router as self_scout_router
from app.routers.settings import router as settings_router
from app.routers.storage import router as storage_router
from app.routers.tracklets import router as tracklets_router
from app.routers.uploads import router as uploads_router
from app.routers.videos import router as videos_router
from app.user_seeding import seed_configured_users

settings = get_settings()
configure_logging(settings.log_level)
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    log.info("startup", environment=settings.environment)
    if settings.seed_users_on_startup:
        admin_email = os.environ.get("SEED_ADMIN_EMAIL", "")
        admin_password = os.environ.get("SEED_ADMIN_PASSWORD", "")
        worker_email = os.environ.get("SEED_WORKER_EMAIL", "")
        worker_password = os.environ.get("SEED_WORKER_PASSWORD", "")
        if (admin_email and admin_password) or (worker_email and worker_password):
            stats = await seed_configured_users(
                database_url=settings.database_url,
                admin_email=admin_email,
                admin_password=admin_password,
                worker_email=worker_email,
                worker_password=worker_password,
                reset_passwords=settings.seed_reset_passwords,
            )
            log.info("startup_user_seeding", **stats, reset_passwords=settings.seed_reset_passwords)
        else:
            log.warning(
                "startup_user_seeding_skipped",
                message=(
                    "SEED_USERS_ON_STARTUP is true but SEED_* credentials are incomplete; "
                    "skipping startup seed pass."
                ),
            )
    from app.scheduler import start_scheduler

    scheduler_task = start_scheduler()
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
        log.info("shutdown")


# Normalized once: "Production"/" production " must gate exactly like
# "production" — a fail-open docs/dev-login surface is not worth a casing typo.
_env = settings.environment.strip().lower()

app = FastAPI(
    title="Football-IQ API",
    description="Backend API for the Toledo Football Computer Vision platform.",
    version="0.1.0",
    lifespan=lifespan,
    # Disable automatic docs in production to reduce attack surface
    docs_url="/docs" if _env != "production" else None,
    redoc_url="/redoc" if _env != "production" else None,
)

# ── Observability middleware ──────────────────────────────────────────────────
app.add_middleware(PrometheusMiddleware)

# ── CORS ─────────────────────────────────────────────────────────────────────
# A deployed frontend served from its real origin is blocked from the API
# unless that origin is allowed here, which manifests as "login doesn't work"
# (the preflight/POST is rejected before it reaches the endpoint). Warn loudly
# when a non-development deployment still only allows localhost.
if (
    _env != "development"
    and settings.cors_origins_list == ["http://localhost:3000"]
    and (not settings.cors_origin_regex)
):
    log.warning(
        "cors_origins_localhost_only",
        message=(
            "CORS_ORIGINS still only allows http://localhost:3000 outside development — "
            "the deployed frontend origin will be blocked. Set CORS_ORIGINS (and/or "
            "CORS_ORIGIN_REGEX) to the frontend's real origin(s)."
        ),
        environment=_env,
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Prometheus metrics endpoint ──────────────────────────────────────────────
@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Expose Prometheus metrics for scraping."""
    return metrics_response()


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(health_workload_router)
app.include_router(health_ingest_router)
app.include_router(auth_router)
# uploads before videos: its literal /download-url and /upload/* paths must
# win over the videos router's dynamic /{video_id} route.
app.include_router(uploads_router)
app.include_router(videos_router)
app.include_router(storage_router)
app.include_router(clips_router)
app.include_router(practice_sessions_router)
app.include_router(players_router)
app.include_router(player_profiles_router)
app.include_router(jobs_router)
app.include_router(calibrations_router)
app.include_router(tracklets_router)
app.include_router(corrections_router)
app.include_router(events_router)
app.include_router(labels_router)
app.include_router(metrics_router)
app.include_router(overlays_router)
app.include_router(mlops_router)
app.include_router(self_scout_router)
app.include_router(opponents_router)
app.include_router(correction_analytics_router)
app.include_router(alerts_sse_router)
app.include_router(alerts_router)
app.include_router(inbox_router)
app.include_router(correction_sync_router)
app.include_router(pose_router)
app.include_router(embeddings_router)
app.include_router(search_router)
app.include_router(concept_proposals_router)
app.include_router(concept_search_router)
app.include_router(settings_router)
app.include_router(reports_router)
app.include_router(cfbd_router)
app.include_router(play_prediction_router)
app.include_router(frontier_analytics_router)
app.include_router(playbook_router)
app.include_router(counterfactuals_router)
