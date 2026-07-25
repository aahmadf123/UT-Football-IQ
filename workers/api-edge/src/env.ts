/** Bindings and variables declared in `wrangler.jsonc`. */

import type { Container } from "@cloudflare/containers";

export interface Env {
  // ── R2 buckets ──────────────────────────────────────────────────────────
  RAW_VIDEO: R2Bucket;
  CLIPS: R2Bucket;
  OVERLAYS: R2Bucket;
  ARTIFACTS: R2Bucket;

  // ── The FastAPI container ───────────────────────────────────────────────
  BACKEND: DurableObjectNamespace<Container>;

  /**
   * Pooled Postgres connection, with query caching disabled on the config.
   *
   * Its `connectionString` is the container's default database URL — see
   * `buildContainerEnv` in `container.ts` for the driver-scheme conversion and
   * for `BACKEND_DATABASE_URL`, which overrides it.
   */
  HYPERDRIVE?: Hyperdrive;

  // ── Secrets (wrangler secret put) ───────────────────────────────────────
  /** Shared with the backend. Verifies JWTs and signed stream URLs. */
  SECRET_KEY: string;
  /** Bearer token the cron handler presents to the internal scheduler route. */
  SCHEDULER_TOKEN: string;
  /**
   * Direct Postgres URL, overriding the Hyperdrive binding.
   *
   * Optional. Set it to bypass Hyperdrive entirely — see the note in
   * `container.ts`.
   */
  BACKEND_DATABASE_URL?: string;
  /** Direct Postgres URL for Alembic (psycopg2). Derived when unset. */
  BACKEND_DATABASE_SYNC_URL?: string;
  /** R2 S3-API credentials the container uses to write clips and artifacts. */
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  /** `https://<account_id>.r2.cloudflarestorage.com` */
  R2_ENDPOINT_URL: string;
  /** Optional bootstrap admin, applied only when SEED_USERS_ON_STARTUP is on. */
  SEED_ADMIN_EMAIL?: string;
  SEED_ADMIN_PASSWORD?: string;
  SEED_WORKER_EMAIL?: string;
  SEED_WORKER_PASSWORD?: string;

  // ── Vars ────────────────────────────────────────────────────────────────
  /** Comma-separated exact origins allowed to call the API from a browser. */
  CORS_ORIGINS: string;
  /** Optional regex for per-deploy preview origins (e.g. Vercel previews). */
  CORS_ORIGIN_REGEX?: string;
  /** Deployment environment label, surfaced by `/edge/health`. */
  ENVIRONMENT?: string;
  /** Public origin of this Worker; the backend mints absolute URLs from it. */
  PUBLIC_API_BASE_URL?: string;
  /** R2 bucket names, passed through to the container's storage facade. */
  R2_BUCKET_RAW?: string;
  R2_BUCKET_CLIPS?: string;
  R2_BUCKET_OVERLAYS?: string;
  R2_BUCKET_ARTIFACTS?: string;
  /** "true" to run the bootstrap seed pass on container start. */
  SEED_USERS_ON_STARTUP?: string;
}
