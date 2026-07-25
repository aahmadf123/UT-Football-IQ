/// <reference types="@cloudflare/vitest-pool-workers/types" />

/**
 * `cloudflare:test` is declared under a subpath export, so it is not picked up
 * by the `types` array in tsconfig.json -- this reference pulls it in for the
 * test files.
 *
 * The bindings below mirror `vitest.config.ts`. Declaring them on
 * `Cloudflare.Env` is what makes `env` from `cloudflare:test` come back typed
 * instead of as an empty object.
 */
declare namespace Cloudflare {
  interface Env {
    RAW_VIDEO: R2Bucket;
    CLIPS: R2Bucket;
    OVERLAYS: R2Bucket;
    ARTIFACTS: R2Bucket;
    SECRET_KEY: string;
    SCHEDULER_TOKEN: string;
    CORS_ORIGINS: string;
    CORS_ORIGIN_REGEX: string;
    ENVIRONMENT: string;
  }
}
