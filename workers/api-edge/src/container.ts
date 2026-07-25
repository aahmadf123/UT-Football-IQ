/**
 * The FastAPI backend, running as a Cloudflare Container.
 *
 * The image is `backend/Dockerfile`. Cloudflare starts an instance on demand
 * and stops it after `sleepAfter` of quiet, so the API costs nothing while
 * nobody is looking at film -- which is most of the week for a college program.
 *
 * A single named instance is used rather than one per session: the backend is
 * stateless apart from its in-process SSE subscriber map, and that map is also
 * why the image runs one uvicorn worker rather than four. See `proxy.ts` and
 * the note in `backend/Dockerfile`.
 */

import { Container } from "@cloudflare/containers";
import type { Env } from "./env";

/**
 * Build the container's environment.
 *
 * Worker secrets and vars are **not** inherited by the container -- the
 * container is a separate sandbox with its own environment, and anything the
 * backend needs has to be handed over explicitly. Silently missing values here
 * do not fail loudly: the app boots and then behaves as though nothing is
 * configured (local disk instead of R2, presigned URLs instead of Worker-served
 * ones, no scheduler credential).
 *
 * Exported so a test can assert the mapping without starting a container.
 */
export function buildContainerEnv(env: Env): Record<string, string> {
  const vars: Record<string, string | undefined> = {
    // ── Database ────────────────────────────────────────────────────────
    // Direct to Postgres, NOT via Hyperdrive: a Hyperdrive connection string
    // only resolves inside the Workers runtime, so the container cannot use it.
    // A long-lived container holds its own SQLAlchemy pool, which is the thing
    // Hyperdrive exists to substitute for.
    DATABASE_URL: env.BACKEND_DATABASE_URL,
    DATABASE_SYNC_URL: env.BACKEND_DATABASE_SYNC_URL ?? toSyncUrl(env.BACKEND_DATABASE_URL),

    // ── Core ────────────────────────────────────────────────────────────
    SECRET_KEY: env.SECRET_KEY,
    ENVIRONMENT: env.ENVIRONMENT ?? "production",
    SCHEDULER_TOKEN: env.SCHEDULER_TOKEN,

    // The in-process asyncio loop cannot fire on a container that sleeps, so
    // the cron trigger in `index.ts` drives the tick instead. Leaving both on
    // would have two schedulers racing for the same advisory lock.
    SCHEDULER_ENABLED: "0",

    // ── Object storage ──────────────────────────────────────────────────
    STORAGE_BACKEND: "s3",
    S3_ACCESS_KEY_ID: env.R2_ACCESS_KEY_ID,
    S3_SECRET_ACCESS_KEY: env.R2_SECRET_ACCESS_KEY,
    S3_ENDPOINT_URL: env.R2_ENDPOINT_URL,
    S3_BUCKET_RAW: env.R2_BUCKET_RAW ?? "footiq-raw-video",
    S3_BUCKET_CLIPS: env.R2_BUCKET_CLIPS ?? "footiq-clips",
    S3_BUCKET_OVERLAYS: env.R2_BUCKET_OVERLAYS ?? "footiq-overlays",
    S3_BUCKET_ARTIFACTS: env.R2_BUCKET_ARTIFACTS ?? "footiq-artifacts",

    // Mint HMAC-signed /api/v1/storage URLs that this Worker serves from R2,
    // rather than S3 presigned URLs. Keeps R2 credentials out of anything a
    // browser sees and gives correct Range handling for <video> scrubbing.
    SIGNED_URL_MODE: "worker",

    // ── Public origin ───────────────────────────────────────────────────
    // Without this the backend mints upload URLs pointing at localhost. It also
    // reads X-Forwarded-* (set in proxy.ts) as a fallback.
    PUBLIC_API_BASE_URL: env.PUBLIC_API_BASE_URL,

    // ── CORS ────────────────────────────────────────────────────────────
    // The Worker answers browser CORS itself, but the backend applies its own
    // middleware too; a mismatch shows up as "login doesn't work".
    CORS_ORIGINS: env.CORS_ORIGINS,
    CORS_ORIGIN_REGEX: env.CORS_ORIGIN_REGEX,

    // ── Bootstrap seeding (optional) ────────────────────────────────────
    SEED_USERS_ON_STARTUP: env.SEED_USERS_ON_STARTUP,
    SEED_ADMIN_EMAIL: env.SEED_ADMIN_EMAIL,
    SEED_ADMIN_PASSWORD: env.SEED_ADMIN_PASSWORD,
    SEED_WORKER_EMAIL: env.SEED_WORKER_EMAIL,
    SEED_WORKER_PASSWORD: env.SEED_WORKER_PASSWORD,
  };

  // Drop unset keys rather than forwarding empty strings: pydantic-settings
  // treats "" as a real value, so an empty SEED_ADMIN_EMAIL would look
  // configured and an empty CORS_ORIGIN_REGEX would compile to a regex that
  // matches everything.
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(vars)) {
    if (value !== undefined && value !== "") out[key] = value;
  }
  return out;
}

/** Derive the psycopg2 URL Alembic wants from the asyncpg one. */
function toSyncUrl(asyncUrl: string | undefined): string | undefined {
  return asyncUrl?.replace("+asyncpg", "");
}

export class BackendContainer extends Container<Env> {
  /** uvicorn's port inside the image. */
  defaultPort = 8000;

  /**
   * Long enough that a coach working through a film session never pays a cold
   * start mid-review, short enough that an idle night is not billed.
   */
  sleepAfter = "20m";

  /**
   * Set from the Worker's own secrets and vars at construction, because none of
   * them cross into the container automatically.
   */
  envVars = buildContainerEnv(this.env);

  override onStart(): void {
    console.log(JSON.stringify({ event: "container_start" }));
  }

  override onStop(): void {
    console.log(JSON.stringify({ event: "container_stop" }));
  }

  override onError(error: unknown): never {
    console.error(
      JSON.stringify({
        event: "container_error",
        error: error instanceof Error ? error.message : String(error),
      }),
    );
    throw error;
  }
}
