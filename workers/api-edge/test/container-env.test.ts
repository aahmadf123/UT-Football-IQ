import { describe, expect, it, vi } from "vitest";
import type { Env } from "../src/env";
import { buildContainerEnv } from "../src/container";

/**
 * Worker secrets do not cross into the container -- it is a separate sandbox
 * with its own environment. Every value the backend needs has to be forwarded
 * explicitly, and a missing one does not fail loudly: the app boots and quietly
 * behaves as if nothing were configured (local disk instead of R2, presigned
 * URLs instead of Worker-served ones, no scheduler credential). That silence is
 * why this mapping is pinned by tests.
 */

const FULL = {
  SECRET_KEY: "shared-secret",
  SCHEDULER_TOKEN: "cron-token",
  BACKEND_DATABASE_URL: "postgresql+asyncpg://u:p@db.example.com/footiq",
  R2_ACCESS_KEY_ID: "r2-key",
  R2_SECRET_ACCESS_KEY: "r2-secret",
  R2_ENDPOINT_URL: "https://acct.r2.cloudflarestorage.com",
  CORS_ORIGINS: "https://app.example.com",
  ENVIRONMENT: "production",
  PUBLIC_API_BASE_URL: "https://api.example.com",
} as unknown as Env;

describe("buildContainerEnv", () => {
  it("forwards everything the backend needs to reach Postgres and R2", () => {
    const out = buildContainerEnv(FULL);
    expect(out.DATABASE_URL).toBe("postgresql+asyncpg://u:p@db.example.com/footiq");
    expect(out.SECRET_KEY).toBe("shared-secret");
    expect(out.SCHEDULER_TOKEN).toBe("cron-token");
    expect(out.S3_ACCESS_KEY_ID).toBe("r2-key");
    expect(out.S3_SECRET_ACCESS_KEY).toBe("r2-secret");
    expect(out.S3_ENDPOINT_URL).toBe("https://acct.r2.cloudflarestorage.com");
  });

  it("derives the Alembic URL from the asyncpg one", () => {
    // Alembic uses psycopg2 and chokes on the +asyncpg driver suffix.
    expect(buildContainerEnv(FULL).DATABASE_SYNC_URL).toBe(
      "postgresql://u:p@db.example.com/footiq",
    );
  });

  it("prefers an explicit sync URL when one is provided", () => {
    const out = buildContainerEnv({
      ...FULL,
      BACKEND_DATABASE_SYNC_URL: "postgresql://readonly@db/footiq",
    } as Env);
    expect(out.DATABASE_SYNC_URL).toBe("postgresql://readonly@db/footiq");
  });

  it("selects the s3 storage backend", () => {
    // Left unset, the facade auto-detects and falls back to local disk inside
    // the container -- where every uploaded clip is lost when it sleeps.
    expect(buildContainerEnv(FULL).STORAGE_BACKEND).toBe("s3");
  });

  it("routes download URLs through the Worker", () => {
    // Keeps R2 credentials out of browser-visible URLs and gives correct Range
    // handling, which is what makes <video> scrubbing work.
    expect(buildContainerEnv(FULL).SIGNED_URL_MODE).toBe("worker");
  });

  it("disables the in-process scheduler", () => {
    // The cron trigger drives the tick. Leaving the asyncio loop on would have
    // two schedulers racing for the same advisory lock -- and on a container
    // that sleeps, the loop cannot fire reliably anyway.
    expect(buildContainerEnv(FULL).SCHEDULER_ENABLED).toBe("0");
  });

  it("defaults the bucket names to the provisioned ones", () => {
    const out = buildContainerEnv(FULL);
    expect(out.S3_BUCKET_RAW).toBe("footiq-raw-video");
    expect(out.S3_BUCKET_CLIPS).toBe("footiq-clips");
    expect(out.S3_BUCKET_OVERLAYS).toBe("footiq-overlays");
    expect(out.S3_BUCKET_ARTIFACTS).toBe("footiq-artifacts");
  });

  it("lets bucket names be overridden", () => {
    const out = buildContainerEnv({ ...FULL, R2_BUCKET_RAW: "other-raw" } as Env);
    expect(out.S3_BUCKET_RAW).toBe("other-raw");
  });

  it("omits unset optional values rather than sending empty strings", () => {
    // pydantic-settings treats "" as a real value: an empty CORS_ORIGIN_REGEX
    // would compile to a regex matching every origin, and an empty
    // SEED_ADMIN_EMAIL would look configured.
    const out = buildContainerEnv({ ...FULL, CORS_ORIGIN_REGEX: "" } as Env);
    expect("CORS_ORIGIN_REGEX" in out).toBe(false);
    expect("SEED_ADMIN_EMAIL" in out).toBe(false);
  });

  it("uses the Hyperdrive connection string by default", () => {
    const { BACKEND_DATABASE_URL: _omit, ...withoutOverride } = FULL as unknown as Record<string, unknown>;
    const out = buildContainerEnv({
      ...withoutOverride,
      HYPERDRIVE: { connectionString: "postgresql://user:pw@hyperdrive.internal/footiq" },
    } as unknown as Env);
    expect(out.DATABASE_URL).toBe("postgresql+asyncpg://user:pw@hyperdrive.internal/footiq");
    expect(out.DATABASE_SYNC_URL).toBe("postgresql://user:pw@hyperdrive.internal/footiq");
  });

  it("normalises the postgres:// scheme Hyperdrive may emit", () => {
    const { BACKEND_DATABASE_URL: _omit, ...withoutOverride } = FULL as unknown as Record<string, unknown>;
    const out = buildContainerEnv({
      ...withoutOverride,
      HYPERDRIVE: { connectionString: "postgres://user:pw@hyperdrive.internal/footiq" },
    } as unknown as Env);
    // SQLAlchemy picks psycopg2 unless the async driver is named, and the async
    // engine then fails at startup.
    expect(out.DATABASE_URL).toBe("postgresql+asyncpg://user:pw@hyperdrive.internal/footiq");
  });

  it("lets BACKEND_DATABASE_URL override Hyperdrive", () => {
    // The recovery path. Cloudflare documents the Hyperdrive string as
    // resolvable only inside the Workers runtime, and the container is a
    // separate sandbox; if it cannot dial it, the symptom looks like a database
    // outage and this secret restores service with no code change.
    const out = buildContainerEnv({
      ...FULL,
      HYPERDRIVE: { connectionString: "postgresql://user:pw@hyperdrive.internal/footiq" },
    } as unknown as Env);
    expect(out.DATABASE_URL).toBe("postgresql+asyncpg://u:p@db.example.com/footiq");
    expect(out.DATABASE_URL).not.toContain("hyperdrive.internal");
  });

  it("does not log the connection string", () => {
    // It carries the database password. Logs go to `wrangler tail` and are
    // retained, so a credential that lands there has effectively leaked.
    const logged: string[] = [];
    const spy = vi.spyOn(console, "log").mockImplementation((...args) => {
      logged.push(args.map(String).join(" "));
    });
    const errSpy = vi.spyOn(console, "error").mockImplementation((...args) => {
      logged.push(args.map(String).join(" "));
    });
    try {
      buildContainerEnv({
        ...FULL,
        BACKEND_DATABASE_URL: undefined,
        HYPERDRIVE: { connectionString: "postgresql://user:sup3rsecret@hyperdrive.internal/db" },
      } as unknown as Env);
    } finally {
      spy.mockRestore();
      errSpy.mockRestore();
    }
    expect(logged.join(" ")).not.toContain("sup3rsecret");
  });

  it("every forwarded value is a non-empty string", () => {
    // The container API takes Record<string, string>; an undefined would be
    // stringified to "undefined" and read as a real setting.
    for (const [key, value] of Object.entries(buildContainerEnv(FULL))) {
      expect(typeof value, key).toBe("string");
      expect(value.length, key).toBeGreaterThan(0);
      expect(value, key).not.toBe("undefined");
    }
  });
});
