/**
 * Cloudflare Workers bindings interface.
 * Matches the bindings declared in wrangler.toml.
 */
export interface Env {
  // R2 buckets
  RAW_VIDEO: R2Bucket;
  CLIPS: R2Bucket;
  OVERLAYS: R2Bucket;
  ARTIFACTS: R2Bucket;

  // Secrets (set with `wrangler secret put`)
  JWT_SECRET: string;
  DATABASE_URL: string;
  BACKEND_API_URL: string;
}
