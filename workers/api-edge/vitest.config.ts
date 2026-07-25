import { defineConfig } from "vitest/config";
import { cloudflareTest } from "@cloudflare/vitest-pool-workers";

/**
 * Tests run inside workerd, not Node, so `crypto.subtle`, `R2Bucket`, and
 * `Request`/`Response` behave exactly as they will in production -- the signing
 * and Range tests would prove nothing against Node shims.
 *
 * Bindings are declared here rather than read from `wrangler.jsonc`: the real
 * config points at a Dockerfile for the backend container, which the test
 * runner has no reason to build. Keep this list in sync with `test/env.d.ts`.
 */
export default defineConfig({
  plugins: [
    cloudflareTest({
      miniflare: {
        compatibilityDate: "2026-06-08",
        compatibilityFlags: ["nodejs_compat"],
        r2Buckets: ["RAW_VIDEO", "CLIPS", "OVERLAYS", "ARTIFACTS"],
        bindings: {
          SECRET_KEY: "test-secret-key",
          SCHEDULER_TOKEN: "test-scheduler-token",
          CORS_ORIGINS: "https://app.example.com,http://localhost:3000",
          CORS_ORIGIN_REGEX: "https://footiq-[a-z0-9-]+\\.vercel\\.app",
          ENVIRONMENT: "test",
        },
      },
    }),
  ],
});
