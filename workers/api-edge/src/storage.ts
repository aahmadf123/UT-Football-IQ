/**
 * Serve objects out of R2 behind an HMAC-signed URL.
 *
 * This is the production implementation of `GET /api/v1/storage/{bucket}/{key}`.
 * The backend implements the same route over local disk for single-box runs, and
 * both accept the identical signed URL, so nothing downstream has to care which
 * one is answering.
 *
 * Range support matters more than it looks: `<video>` scrubbing is entirely
 * Range requests, and a server that ignores them forces the browser to download
 * the whole file before it can seek.
 */

import type { Env } from "./env";
import { isValidKey, verifyStreamSignature } from "./signing";

/** Buckets reachable through the streaming endpoint. */
const DOWNLOADABLE_BUCKETS = new Set(["raw-video", "clips", "overlays"]);

/**
 * `artifacts` is deliberately excluded, matching `DOWNLOADABLE_BUCKETS` in
 * `backend/app/storage.py`: reports are reached only through
 * `GET /api/v1/reports/{id}/download`, which applies ownership checks first.
 */
export function bucketBinding(env: Env, bucket: string): R2Bucket | null {
  if (!DOWNLOADABLE_BUCKETS.has(bucket)) return null;
  switch (bucket) {
    case "raw-video":
      return env.RAW_VIDEO;
    case "clips":
      return env.CLIPS;
    case "overlays":
      return env.OVERLAYS;
    default:
      return null;
  }
}

/** Any bucket, including write-only ones. Used by the upload path. */
export function writableBucketBinding(env: Env, bucket: string): R2Bucket | null {
  if (bucket === "artifacts") return env.ARTIFACTS;
  return bucketBinding(env, bucket);
}

interface ParsedRange {
  offset: number;
  length?: number;
  suffix?: number;
}

/**
 * Parse a single-range `Range: bytes=...` header.
 *
 * Multi-range requests are intentionally unsupported -- they require a
 * multipart/byteranges response and no browser media element sends them.
 * Returning `null` makes the caller fall back to a normal 200.
 */
export function parseRange(header: string | null): ParsedRange | null {
  if (!header) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!match) return null;
  const [, startRaw, endRaw] = match;

  if (startRaw === "" && endRaw === "") return null;

  // "bytes=-500" -- the trailing N bytes.
  if (startRaw === "") {
    const suffix = Number(endRaw);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) return null;
    return { offset: 0, suffix };
  }

  const start = Number(startRaw);
  if (!Number.isSafeInteger(start) || start < 0) return null;

  // "bytes=100-" -- from an offset to the end.
  if (endRaw === "") return { offset: start };

  const end = Number(endRaw);
  if (!Number.isSafeInteger(end) || end < start) return null;
  return { offset: start, length: end - start + 1 };
}

function cacheControlFor(bucket: string): string {
  // Objects are content-addressed by an upload timestamp and never rewritten in
  // place, so they are safe to cache hard. The signed URL's own expiry is what
  // bounds access, not the cache lifetime.
  return bucket === "raw-video" ? "private, max-age=3600" : "private, max-age=86400";
}

export async function handleStorageGet(
  request: Request,
  env: Env,
  bucket: string,
  key: string,
): Promise<Response> {
  if (!isValidKey(key)) {
    return json({ detail: "Invalid storage key" }, 400);
  }

  const binding = bucketBinding(env, bucket);
  if (!binding) {
    // Same 404 for "unknown bucket" and "not downloadable" -- do not confirm
    // that `artifacts` exists to an unauthenticated caller.
    return json({ detail: "Not found" }, 404);
  }

  const url = new URL(request.url);
  const check = await verifyStreamSignature(
    env.SECRET_KEY,
    bucket,
    key,
    url.searchParams.get("exp"),
    url.searchParams.get("sig"),
  );
  if (!check.ok) {
    const status = check.reason === "expired" ? 410 : 403;
    return json({ detail: `Signature ${check.reason}` }, status);
  }

  const range = parseRange(request.headers.get("Range"));

  // HEAD is what players use to discover length and Range support.
  if (request.method === "HEAD") {
    const head = await binding.head(key);
    if (!head) return json({ detail: "Not found" }, 404);
    const headers = baseHeaders(head, bucket);
    headers.set("Content-Length", String(head.size));
    return new Response(null, { status: 200, headers });
  }

  const object = await binding.get(key, range ? { range: toR2Range(range) } : undefined);
  if (!object) return json({ detail: "Not found" }, 404);

  const headers = baseHeaders(object, bucket);

  if (!range) {
    headers.set("Content-Length", String(object.size));
    return new Response(object.body, { status: 200, headers });
  }

  // `object.range` is what R2 actually served, which is what the Content-Range
  // header must describe -- an over-long client range is clamped, not rejected.
  const served = object.range as { offset?: number; length?: number } | undefined;
  const offset = served?.offset ?? 0;
  const length = served?.length ?? object.size - offset;
  const end = offset + length - 1;

  if (offset >= object.size) {
    return new Response(null, {
      status: 416,
      headers: { "Content-Range": `bytes */${object.size}`, "Accept-Ranges": "bytes" },
    });
  }

  headers.set("Content-Range", `bytes ${offset}-${end}/${object.size}`);
  headers.set("Content-Length", String(length));
  return new Response(object.body, { status: 206, headers });
}

function toR2Range(range: ParsedRange): R2Range {
  if (range.suffix !== undefined) return { suffix: range.suffix };
  if (range.length !== undefined) return { offset: range.offset, length: range.length };
  return { offset: range.offset };
}

function baseHeaders(object: R2Object, bucket: string): Headers {
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/octet-stream");
  }
  headers.set("ETag", object.httpEtag);
  headers.set("Accept-Ranges", "bytes");
  headers.set("Cache-Control", cacheControlFor(bucket));
  return headers;
}

export function json(body: unknown, status = 200, extra?: HeadersInit): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...(extra ?? {}) },
  });
}
