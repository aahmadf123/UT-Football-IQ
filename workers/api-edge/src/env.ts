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

  // ── Secrets (wrangler secret put) ───────────────────────────────────────
  /** Shared with the backend. Verifies JWTs and signed stream URLs. */
  SECRET_KEY: string;
  /** Bearer token the cron handler presents to the internal scheduler route. */
  SCHEDULER_TOKEN: string;

  // ── Vars ────────────────────────────────────────────────────────────────
  /** Comma-separated exact origins allowed to call the API from a browser. */
  CORS_ORIGINS: string;
  /** Optional regex for per-deploy preview origins (e.g. Vercel previews). */
  CORS_ORIGIN_REGEX?: string;
  /** Deployment environment label, surfaced by `/edge/health`. */
  ENVIRONMENT?: string;
}
