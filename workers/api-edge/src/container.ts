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
  const databaseUrl = resolveDatabaseUrl(env);

  const vars: Record<string, string | undefined> = {
    // ── Database ────────────────────────────────────────────────────────
    // Hyperdrive hands out a `postgres(ql)://` URL. SQLAlchemy needs the async
    // driver spelled out, and Alembic needs the plain psycopg2 form.
    DATABASE_URL: toAsyncUrl(databaseUrl),
    DATABASE_SYNC_URL: env.BACKEND_DATABASE_SYNC_URL ?? toSyncUrl(databaseUrl),

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

/**
 * Pick the Postgres URL the container will dial, or fail loudly.
 *
 * `BACKEND_DATABASE_URL` is the supported source, not a fallback. Cloudflare
 * documents the Hyperdrive connection string as "only accessible from your
 * Worker": the hostname is resolved inside the Workers runtime, and a container
 * is a separate sandbox whose egress goes to the public internet. So the
 * Hyperdrive string, handed to the container, points at nothing it can reach.
 *
 * The binding is still read as a last resort rather than ignored -- if that
 * ever changes, or the config is fronted by a reachable address, this keeps
 * working -- but taking that branch is warned about, because the failure it
 * produces (asyncpg timing out at startup) reads like a database outage rather
 * than a misconfiguration.
 *
 * Throwing here surfaces at container construction, which is the point: the
 * alternative is a backend that boots, serves 500s on every route that touches
 * Postgres, and gives no clue which of a dozen settings is missing.
 */
function resolveDatabaseUrl(env: Env): string {
  if (env.BACKEND_DATABASE_URL) return env.BACKEND_DATABASE_URL;

  const hyperdrive = env.HYPERDRIVE?.connectionString;
  if (hyperdrive) {
    // Deliberately no URL in the payload -- it carries the database password,
    // and `wrangler tail` output is retained.
    console.warn(
      JSON.stringify({
        event: "container_db_url_from_hyperdrive",
        detail:
          "Falling back to the Hyperdrive binding. Its connection string is " +
          "documented as resolvable only inside the Workers runtime, so the " +
          "container is likely to fail to connect. Set the " +
          "BACKEND_DATABASE_URL secret to the origin Postgres URL.",
      }),
    );
    return hyperdrive;
  }

  throw new Error(
    "No database URL configured for the backend container. Run " +
      "`wrangler secret put BACKEND_DATABASE_URL` with the origin Postgres URL.",
  );
}

/**
 * A URL whose scheme already names a driver, e.g. `postgresql+asyncpg://`.
 *
 * Anchored to the scheme on purpose. Testing the whole URL for `+` misreads a
 * password containing one -- common in generated credentials -- as an
 * already-qualified scheme, skips normalisation, and leaves SQLAlchemy to pick
 * psycopg2 and fail the async engine at startup.
 */
const DRIVER_SCHEME_RE = /^[a-z][a-z0-9.-]*\+[a-z0-9_]+:\/\//i;

/**
 * Normalise any Postgres URL to the async driver SQLAlchemy expects.
 *
 * Hyperdrive emits `postgresql://` (and `postgres://` is equally valid), while
 * SQLAlchemy needs the driver named explicitly or it picks psycopg2 and the
 * async engine fails at startup. Already-qualified URLs pass through untouched
 * so an operator override can name its own driver.
 */
function toAsyncUrl(url: string): string {
  if (DRIVER_SCHEME_RE.test(url)) return url;
  return url.replace(/^postgres(ql)?:\/\//, "postgresql+asyncpg://");
}

/** Normalise to the plain psycopg2 form Alembic wants. */
function toSyncUrl(url: string): string {
  return url.replace(/^postgres(ql)?(\+\w+)?:\/\//, "postgresql://");
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
