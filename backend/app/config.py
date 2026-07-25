"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Core ──────────────────────────────────────────────────────────────
    environment: str = "development"
    secret_key: str
    log_level: str = "INFO"

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str  # asyncpg URL for the app
    database_sync_url: str = ""  # psycopg2 URL for Alembic (derived if empty)

    # ── Object storage (any S3-compatible provider) ───────────────────────
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_endpoint_url: str = ""
    s3_bucket_raw: str = "raw-video"
    s3_bucket_clips: str = "clips"
    s3_bucket_overlays: str = "overlays"
    s3_bucket_artifacts: str = "artifacts"
    s3_presign_ttl: int = 3600

    # ── Storage backend selection ─────────────────────────────────────────
    # "s3" | "local" | "" (auto: s3 when object-store credentials are
    # configured, otherwise local). "local" stores objects under
    # ``local_storage_root`` as ``{root}/{bucket}/{key}`` with
    # ``local://bucket/key`` URIs and serves them through the HMAC-signed
    # /api/v1/storage route, so the whole platform runs with no cloud account.
    storage_backend: str = ""
    local_storage_root: str = "./data/storage"
    # Absolute base used when minting signed local download URLs (the
    # frontend <video> tag needs a resolvable origin, not a relative path).
    public_api_base_url: str = "http://localhost:8000"
    # Ceiling for a single PUT /api/v1/videos/upload/{key} body. Full-game
    # 4K film stays well under this; the cap exists so an authenticated
    # client cannot fill the storage volume with one endless stream.
    max_upload_bytes: int = 8 * 1024 * 1024 * 1024

    # ── JWT ───────────────────────────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30

    # ── CORS ──────────────────────────────────────────────────────────────
    # Comma-separated exact allowed origins. Defaults cover local development
    # only. Any deployed frontend origin MUST be added here (via CORS_ORIGINS)
    # or every browser call to the API — including login/register — is blocked
    # by CORS.
    cors_origins: str = "http://localhost:3000"
    # Optional regex for origins that vary per deploy (e.g. per-branch preview
    # URLs). Empty by default: a deployment opts in explicitly rather than
    # inheriting a pattern that matches somebody else's hosting.
    # Example: r"^https://myapp-[a-z0-9-]+\.example\.com$"
    cors_origin_regex: str = ""

    # ── Same-session feedback loop (Issue #147) ───────────────────────────
    # Clip ``confidence`` at or below this value is surfaced to coaches as
    # ``low_confidence`` in the derived review state. Calibrated high
    # uncertainty (Issue #146) is also treated as low-confidence. Tunable so
    # staff can tighten/loosen the first-pass review queue without a redeploy.
    clip_low_confidence_threshold: float = 0.5

    # ── Player development passport (Issue #7) ────────────────────────────
    # A development snapshot whose resolved identity confidence is below this
    # value (or whose identity state is not known/probable) is forced
    # experimental and can never be approved for player-facing use. Jersey OCR
    # is never trusted as ground truth, so this gate keeps low-confidence
    # identities from being silently attached to a player profile.
    player_profile_identity_confidence_threshold: float = 0.5

    # ── College Football Data (CFBD) — backend-only (Issues #160/#161/#162) ─
    # Vendor integration for Toledo/MAC analytics. The key is read by the
    # backend ONLY and must never reach frontend code, browser bundles, logs,
    # or coach-visible errors. ``cfbd_api_key`` defaults to empty so the app
    # boots without it; the client fails safely with a backend-only error when
    # a CFBD call is attempted without a key (see app.cfbd.client).
    cfbd_api_key: str = ""
    cfbd_base_url: str = "https://api.collegefootballdata.com"

    # Kaggle API settings
    kaggle_username: str = ""
    kaggle_api_token: str = ""

    # Cached CFBD rows older than this are flagged ``stale`` to the UI. College
    # football data refreshes roughly weekly, so the default is 7 days.
    cfbd_cache_stale_after_hours: int = 168

    # ── Active-learning queue (Issues #145 / #146) ─────────────────────────
    # Tunables for the calibrated-uncertainty prioritisation policy in
    # ``app.active_learning`` (consumed by the MLOps nightly sampler). All are
    # read by the backend only; see docs/active-learning.md.
    # Blend weight on normalised entropy vs. (1 - calibrated confidence) for a
    # calibrated signal (0 = confidence only, 1 = entropy only).
    active_learning_entropy_weight: float = 0.5
    # Fixed priority floor for an uncalibrated output that carries no usable
    # ranking signal — kept reviewable, never shown as confident.
    active_learning_uncalibrated_priority: float = 0.6
    # Fallback priority for a model output with no uncertainty signal at all.
    active_learning_missing_priority: float = 0.5

    # ── Zero-shot concept search (Issue #144) ─────────────────────────────
    # When true, ``GET /api/v1/concept-search`` may expand its grounded
    # structured-label matches with an *experimental* pgvector similarity pass
    # over the existing play embeddings (Issue #8) to surface visually similar
    # but not-yet-labelled reps. Expansion results are always flagged
    # experimental/approximate. Set false to restrict concept search to grounded
    # metadata matches only (no new vector store is ever introduced either way).
    concept_search_embedding_expansion: bool = True

    # ── Playbook overlays & assignment scoring (Issue #15) ────────────────
    # When false, the playbook router returns 404 for every route so the
    # feature can be dark-launched without un-mounting the endpoints.
    playbook_enabled: bool = True
    # Resolved player identity confidence below this value (or an identity
    # state that is not known/probable) downgrades an assignment grade to
    # "needs_review" instead of a negative ("off assignment") grade — a
    # low-confidence identity is never scored as a wrong assignment.
    playbook_identity_confidence_threshold: float = 0.6
    # Upper bound on clips the corpus (re)scoring heavy endpoint will scan.
    playbook_score_corpus_max_plays: int = 500

    # ── Counterfactual coverage simulator (Issue #141) ────────────────────
    # Offline/backend MVP only — there is NO coach-facing "What-if" frontend
    # yet (blocked until calibrated uncertainty #146 + the IA decisions). When
    # false the /api/v1/counterfactuals surface 404s entirely (dark-launch
    # guard); the endpoint is coaching-staff only and every response is
    # experimental regardless.
    counterfactual_simulator_enabled: bool = True
    # Cap on clips the workload-gated simulator will scan when building the
    # (route × coverage) → expected-yards lookup from the labeled corpus.
    counterfactual_corpus_max_plays: int = 1000
    # Caller-supplied input-quality signals (identity / tracking / calibration
    # confidence) at or below this value force the response to
    # experimental-only, concept-level language — a low-confidence substrate
    # never yields trusted coach-facing counterfactual language.
    counterfactual_input_confidence_threshold: float = 0.6

    # ── Auth bootstrap seeding (ops) ──────────────────────────────────────
    # Optional startup seed pass for cloud deployments where auth users must
    # exist immediately after boot. Disabled by default for safety.
    seed_users_on_startup: bool = False
    # When true and seeding is enabled, rotate passwords for configured seed
    # users on startup. Keep false unless intentionally recovering access.
    seed_reset_passwords: bool = False

    @field_validator("database_sync_url", mode="before")
    @classmethod
    def derive_sync_url(cls, v: str, info: Any) -> str:
        if v:
            return v
        # Derive from async URL by stripping driver suffix
        async_url: str = ""
        if hasattr(info, "data"):
            async_url = str(info.data.get("database_url", ""))
        return async_url.replace("+asyncpg", "")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()  # type: ignore[call-arg]
