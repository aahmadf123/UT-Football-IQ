/**
 * Cloudflare R2 helpers — upload proxy, signed streaming URLs, and object retrieval.
 *
 * Upload flow:
 *   1. Client calls POST /api/v1/videos/upload-url -> { uploadUrl, key }
 *   2. Client PUTs the file body to the returned uploadUrl (Worker proxy)
 *   3. Worker writes the body to the R2 bucket via its binding
 *
 * Download / playback flow:
 *   1. Client calls GET /api/v1/videos/download-url?bucket=raw-video&key=raw/...
 *      (authenticated via Bearer JWT) -> { downloadUrl }
 *   2. downloadUrl is an HMAC-signed Worker URL:
 *      /dl/<bucket>/<key>?exp=<unix>&sig=<hex>
 *   3. <video src={downloadUrl}> fetches the URL; the Worker verifies the
 *      signature, reads the object from R2, and streams it with proper
 *      Content-Type, Content-Length, Accept-Ranges, and Range support.
 */

import type { Env } from "./types.js";

export type R2BucketName = "raw-video" | "clips" | "overlays" | "artifacts";

/** Buckets the download endpoint is allowed to serve. */
const DOWNLOADABLE_BUCKETS: ReadonlySet<string> = new Set<string>([
  "raw-video",
  "clips",
  "overlays",
]);

export function isDownloadableBucket(
  name: string,
): name is "raw-video" | "clips" | "overlays" {
  return DOWNLOADABLE_BUCKETS.has(name);
}

export function getBucket(env: Env, bucketName: R2BucketName): R2Bucket {
  switch (bucketName) {
    case "raw-video":
      return env.RAW_VIDEO;
    case "clips":
      return env.CLIPS;
    case "overlays":
      return env.OVERLAYS;
    case "artifacts":
      return env.ARTIFACTS;
  }
}

// ── Upload helpers ───────────────────────────────────────────────────────────

export function buildUploadProxyUrl(requestUrl: string, key: string): string {
  const base = new URL(requestUrl);
  return `${base.origin}/api/v1/videos/upload/${encodeURIComponent(key)}`;
}

export async function putObject(
  env: Env,
  bucketName: R2BucketName,
  key: string,
  body: ReadableStream | ArrayBuffer | null,
  contentType?: string,
): Promise<R2Object> {
  const bucket = getBucket(env, bucketName);
  const httpMetadata: R2HTTPMetadata = {};
  if (contentType) {
    httpMetadata.contentType = contentType;
  }
  return bucket.put(key, body, { httpMetadata });
}

// ── Signed-URL helpers ───────────────────────────────────────────────────────

function hexEncode(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function deriveStreamKey(secret: string): Promise<CryptoKey> {
  const enc = new TextEncoder();
  const baseKey = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const derived = await crypto.subtle.sign("HMAC", baseKey, enc.encode("football-iq:stream-signing"));
  return crypto.subtle.importKey(
    "raw",
    derived,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

async function hmacSign(secret: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await deriveStreamKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return hexEncode(sig);
}

function hexDecode(hex: string): Uint8Array | null {
  if (hex.length === 0 || hex.length % 2 !== 0) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    const byte = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    if (isNaN(byte)) return null;
    out[i] = byte;
  }
  return out;
}

async function hmacVerify(secret: string, message: string, sigHex: string): Promise<boolean> {
  const sigBytes = hexDecode(sigHex);
  if (!sigBytes) return false;
  const enc = new TextEncoder();
  const key = await deriveStreamKey(secret);
  return crypto.subtle.verify("HMAC", key, sigBytes, enc.encode(message));
}

/**
 * Validate that a key does not attempt path traversal.
 * Rejects keys containing `..`, leading `/`, or null bytes.
 */
export function isValidKey(key: string): boolean {
  if (!key || key.length === 0) return false;
  if (key.includes("\0")) return false;
  if (key.startsWith("/")) return false;
  if (key.includes("..")) return false;
  return true;
}

/**
 * Build an HMAC-signed streaming URL for a given bucket + key.
 * The URL is valid for `ttlSeconds` (default 1 hour).
 */
export async function createSignedStreamUrl(
  env: Env,
  requestUrl: string,
  bucketName: string,
  key: string,
  ttlSeconds = 3600,
): Promise<string> {
  const exp = Math.floor(Date.now() / 1000) + ttlSeconds;
  const message = `${bucketName}:${key}:${exp}`;
  const sig = await hmacSign(env.JWT_SECRET, message);
  const base = new URL(requestUrl);
  const encodedKey = key
    .split("/")
    .map((s) => encodeURIComponent(s))
    .join("/");
  const dlUrl = new URL(`/dl/${encodeURIComponent(bucketName)}/${encodedKey}`, base.origin);
  dlUrl.searchParams.set("exp", String(exp));
  dlUrl.searchParams.set("sig", sig);
  return dlUrl.toString();
}

/**
 * Verify an HMAC signature for a signed streaming URL.
 */
export async function verifySignedUrl(
  env: Env,
  bucketName: string,
  key: string,
  exp: string,
  sig: string,
): Promise<boolean> {
  const expNum = parseInt(exp, 10);
  if (isNaN(expNum) || expNum < Math.floor(Date.now() / 1000)) {
    return false;
  }
  const message = `${bucketName}:${key}:${exp}`;
  return hmacVerify(env.JWT_SECRET, message, sig);
}

/**
 * Retrieve an R2 object, optionally with a Range header for partial content.
 */
export async function getObject(
  env: Env,
  bucketName: "raw-video" | "clips" | "overlays",
  key: string,
  rangeHeader?: string | null,
): Promise<R2ObjectBody | null> {
  const bucket = getBucket(env, bucketName);
  const options: R2GetOptions = {};
  if (rangeHeader) {
    const parsed = parseRangeHeader(rangeHeader);
    if (parsed) {
      options.range = parsed;
    }
  }
  const obj = await bucket.get(key, options);
  return obj;
}

/**
 * Parse an HTTP Range header into R2's range format.
 * Supports `bytes=start-end` and `bytes=start-`.
 */
function parseRangeHeader(
  header: string,
): { offset: number; length?: number } | undefined {
  const match = /^bytes=(\d+)-(\d*)$/.exec(header.trim());
  if (!match) return undefined;
  const start = parseInt(match[1], 10);
  if (start < 0) return undefined;
  if (match[2]) {
    const end = parseInt(match[2], 10);
    if (end < start) return undefined;
    const length = end - start + 1;
    if (length <= 0) return undefined;
    return { offset: start, length };
  }
  return { offset: start };
}
