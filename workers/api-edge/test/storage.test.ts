import { env } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import { parseRange } from "../src/storage";
import { signStreamPath } from "../src/signing";
import { route } from "../src/index";

const testEnv = env as Env;
const SECRET = "test-secret-key";
const FUTURE = Math.floor(Date.now() / 1000) + 3600;
const PAST = Math.floor(Date.now() / 1000) - 10;

/** 1 KiB of recognisable bytes so range slices can be checked by value. */
const BODY = new Uint8Array(1024).map((_, i) => i % 251);

const KEY = "raw/1750000000000-practice.mp4";
const KEY_WITH_SPACES = "clips/period 2/play 07.mp4";

async function signedUrl(bucket: string, key: string, exp = FUTURE): Promise<string> {
  const sig = await signStreamPath(SECRET, bucket, key, exp);
  const encoded = key.split("/").map(encodeURIComponent).join("/");
  return `https://edge.test/api/v1/storage/${encodeURIComponent(bucket)}/${encoded}?exp=${exp}&sig=${sig}`;
}

beforeAll(async () => {
  await testEnv.RAW_VIDEO.put(KEY, BODY, {
    httpMetadata: { contentType: "video/mp4" },
  });
  await testEnv.CLIPS.put(KEY_WITH_SPACES, BODY, {
    httpMetadata: { contentType: "video/mp4" },
  });
  await testEnv.ARTIFACTS.put("reports/secret.pdf", BODY);
});

describe("parseRange", () => {
  it("parses a bounded range", () => {
    expect(parseRange("bytes=0-99")).toEqual({ offset: 0, length: 100 });
    expect(parseRange("bytes=100-199")).toEqual({ offset: 100, length: 100 });
  });

  it("parses an open-ended range", () => {
    expect(parseRange("bytes=500-")).toEqual({ offset: 500 });
  });

  it("parses a suffix range", () => {
    expect(parseRange("bytes=-256")).toEqual({ offset: 0, suffix: 256 });
  });

  it.each([
    null,
    "",
    "bytes=",
    "bytes=-",
    "items=0-10",
    "bytes=abc-def",
    "bytes=200-100", // end before start
    "bytes=0-10, 20-30", // multi-range: deliberately unsupported
  ])("returns null for %s", (header) => {
    expect(parseRange(header)).toBeNull();
  });
});

describe("GET /api/v1/storage", () => {
  it("serves a whole object with a valid signature", async () => {
    const res = await route(new Request(await signedUrl("raw-video", KEY)), testEnv);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("video/mp4");
    expect(res.headers.get("Accept-Ranges")).toBe("bytes");
    expect(res.headers.get("Content-Length")).toBe("1024");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(BODY);
  });

  it("serves a byte range as 206 with the right slice", async () => {
    const res = await route(
      new Request(await signedUrl("raw-video", KEY), { headers: { Range: "bytes=100-199" } }),
      testEnv,
    );
    expect(res.status).toBe(206);
    expect(res.headers.get("Content-Range")).toBe("bytes 100-199/1024");
    expect(res.headers.get("Content-Length")).toBe("100");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(BODY.slice(100, 200));
  });

  it("serves a suffix range", async () => {
    const res = await route(
      new Request(await signedUrl("raw-video", KEY), { headers: { Range: "bytes=-64" } }),
      testEnv,
    );
    expect(res.status).toBe(206);
    expect(res.headers.get("Content-Range")).toBe("bytes 960-1023/1024");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(BODY.slice(960));
  });

  it("serves an open-ended range", async () => {
    const res = await route(
      new Request(await signedUrl("raw-video", KEY), { headers: { Range: "bytes=1000-" } }),
      testEnv,
    );
    expect(res.status).toBe(206);
    expect(res.headers.get("Content-Range")).toBe("bytes 1000-1023/1024");
  });

  it("answers HEAD with the length and no body", async () => {
    const res = await route(
      new Request(await signedUrl("raw-video", KEY), { method: "HEAD" }),
      testEnv,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Length")).toBe("1024");
    expect(res.headers.get("Accept-Ranges")).toBe("bytes");
    expect(await res.text()).toBe("");
  });

  it("handles percent-encoded keys with spaces", async () => {
    // The DJI filenames all contain spaces, so this is the common case, not an
    // edge case: the signature is over the decoded key.
    const res = await route(new Request(await signedUrl("clips", KEY_WITH_SPACES)), testEnv);
    expect(res.status).toBe(200);
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(BODY);
  });

  it("rejects an unsigned request", async () => {
    const res = await route(
      new Request(`https://edge.test/api/v1/storage/raw-video/${KEY}`),
      testEnv,
    );
    expect(res.status).toBe(403);
  });

  it("rejects a tampered signature", async () => {
    const url = new URL(await signedUrl("raw-video", KEY));
    url.searchParams.set("sig", "0".repeat(64));
    expect((await route(new Request(url), testEnv)).status).toBe(403);
  });

  it("returns 410 for an expired signature", async () => {
    // Distinct from 403 so the client knows to ask for a fresh URL rather than
    // treating it as a permissions problem.
    const res = await route(new Request(await signedUrl("raw-video", KEY, PAST)), testEnv);
    expect(res.status).toBe(410);
  });

  it("refuses to serve the artifacts bucket even when correctly signed", async () => {
    // Reports carry per-user ownership checks that only the backend can apply.
    const res = await route(new Request(await signedUrl("artifacts", "reports/secret.pdf")), testEnv);
    expect(res.status).toBe(404);
  });

  it("404s a signed but missing object", async () => {
    const res = await route(await signedRequest("raw-video", "raw/nope.mp4"), testEnv);
    expect(res.status).toBe(404);
  });

  it.each([
    ["plain", "raw-video/raw/../../etc/passwd"],
    ["percent-encoded", "raw-video/raw/%2e%2e/%2e%2e/etc/passwd"],
  ])("collapses a %s traversal before it can become a key", async (_label, path) => {
    // The WHATWG URL parser resolves dot segments during construction, and
    // treats %2e as a dot while doing so. Both forms therefore arrive as
    // /api/v1/storage/etc/passwd -- bucket "etc", which is not downloadable.
    // Traversal cannot survive the path into the key decoder at all; the
    // isValidKey check on this route is defence in depth, not the only guard.
    const url = new URL(`https://edge.test/api/v1/storage/${path}`);
    expect(url.pathname).toBe("/api/v1/storage/etc/passwd");
    expect((await route(new Request(url), testEnv)).status).toBe(404);
  });

  it("rejects a malformed percent-escape", async () => {
    const res = await route(
      new Request("https://edge.test/api/v1/storage/raw-video/%zz?exp=1&sig=x"),
      testEnv,
    );
    expect(res.status).toBe(400);
  });

  it("rejects a write method", async () => {
    const res = await route(
      new Request(await signedUrl("raw-video", KEY), { method: "DELETE" }),
      testEnv,
    );
    expect(res.status).toBe(405);
  });
});

async function signedRequest(bucket: string, key: string): Promise<Request> {
  return new Request(await signedUrl(bucket, key));
}

describe("GET /edge/health", () => {
  it("answers without touching the container", async () => {
    const res = await route(new Request("https://edge.test/edge/health"), testEnv);
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ status: "ok", environment: "test" });
  });
});
