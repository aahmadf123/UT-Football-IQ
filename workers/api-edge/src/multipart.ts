/**
 * R2 multipart upload, driven from the browser.
 *
 * Replaces the single-shot `PUT` through the API. That path buffered whole files
 * in the backend, capped at 8 GiB, and could not resume -- a dropped connection
 * 90% through a practice upload meant starting over. Multipart uploads parts
 * concurrently, retries only the failed part, and never routes bytes through the
 * container.
 *
 * Flow:
 *   POST   /api/v1/uploads/multipart/create    -> { key, uploadId }
 *   PUT    /api/v1/uploads/multipart/part      -> { partNumber, etag }
 *   POST   /api/v1/uploads/multipart/complete  -> { storageUri, size }
 *   DELETE /api/v1/uploads/multipart/abort     -> 204
 *
 * Every call requires a valid access token. The Worker checks the signature so
 * an anonymous caller cannot spend R2 writes; the container remains the
 * authority on *who may upload what* when the resulting video is registered.
 */

import type { Env } from "./env";
import { bearerToken, verifyAccessToken } from "./auth";
import { isValidKey } from "./signing";
import { json, writableBucketBinding } from "./storage";

/** R2 requires every part except the last to be at least 5 MiB. */
export const MIN_PART_SIZE = 5 * 1024 * 1024;
/** R2 allows at most 10,000 parts per upload. */
export const MAX_PARTS = 10_000;

/** Only raw film is uploadable from a browser. Derived assets are worker-written. */
const UPLOADABLE_BUCKETS = new Set(["raw-video"]);

export interface UploadedPart {
  partNumber: number;
  etag: string;
}

/**
 * Build the object key for an upload.
 *
 * Matches the backend's `raw/{epoch_ms}-{safe_filename}` shape so both writers
 * produce keys that sort by upload time and never collide on a repeated
 * filename. The filename is sanitised here because it comes from the client.
 */
export function buildObjectKey(filename: string, nowMs: number): string {
  const base = filename.split(/[\\/]/).pop() ?? "upload";
  const safe = base
    .replace(/[^A-Za-z0-9._-]/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^[-.]+/, "")
    .slice(0, 180);
  return `raw/${nowMs}-${safe || "upload"}`;
}

async function requireAuth(request: Request, env: Env): Promise<Response | null> {
  const result = await verifyAccessToken(bearerToken(request), env.SECRET_KEY);
  if (!result.ok) {
    return json({ detail: "Not authenticated", reason: result.reason }, 401);
  }
  return null;
}

function resolveBucket(env: Env, bucket: string): R2Bucket | null {
  if (!UPLOADABLE_BUCKETS.has(bucket)) return null;
  return writableBucketBinding(env, bucket);
}

export async function handleMultipartCreate(request: Request, env: Env): Promise<Response> {
  const denied = await requireAuth(request, env);
  if (denied) return denied;

  let body: { filename?: string; bucket?: string; contentType?: string };
  try {
    body = await request.json();
  } catch {
    return json({ detail: "Body must be JSON" }, 400);
  }

  const bucketName = body.bucket ?? "raw-video";
  const bucket = resolveBucket(env, bucketName);
  if (!bucket) return json({ detail: `Bucket ${bucketName} is not uploadable` }, 400);

  if (!body.filename) return json({ detail: "filename is required" }, 400);

  const key = buildObjectKey(body.filename, Date.now());
  if (!isValidKey(key)) return json({ detail: "Invalid storage key" }, 400);

  // Refuse to clobber. Keys carry a millisecond timestamp so this is close to
  // impossible, but silently overwriting somebody's film is not a failure mode
  // worth leaving open.
  if (await bucket.head(key)) {
    return json({ detail: "Object already exists" }, 409);
  }

  const upload = await bucket.createMultipartUpload(key, {
    httpMetadata: body.contentType ? { contentType: body.contentType } : undefined,
  });

  return json({
    key: upload.key,
    uploadId: upload.uploadId,
    bucket: bucketName,
    minPartSize: MIN_PART_SIZE,
    maxParts: MAX_PARTS,
  });
}

export async function handleMultipartPart(request: Request, env: Env): Promise<Response> {
  const denied = await requireAuth(request, env);
  if (denied) return denied;

  const url = new URL(request.url);
  const key = url.searchParams.get("key");
  const uploadId = url.searchParams.get("uploadId");
  const partNumberRaw = url.searchParams.get("partNumber");
  const bucketName = url.searchParams.get("bucket") ?? "raw-video";

  if (!key || !uploadId || !partNumberRaw) {
    return json({ detail: "key, uploadId and partNumber are required" }, 400);
  }
  if (!isValidKey(key)) return json({ detail: "Invalid storage key" }, 400);

  const partNumber = Number(partNumberRaw);
  if (!Number.isSafeInteger(partNumber) || partNumber < 1 || partNumber > MAX_PARTS) {
    return json({ detail: `partNumber must be 1..${MAX_PARTS}` }, 400);
  }

  const bucket = resolveBucket(env, bucketName);
  if (!bucket) return json({ detail: `Bucket ${bucketName} is not uploadable` }, 400);

  if (!request.body) return json({ detail: "Request body is required" }, 400);

  const upload = bucket.resumeMultipartUpload(key, uploadId);
  try {
    // Streamed straight through -- the part never lands in Worker memory, which
    // is what keeps this inside the 128 MB isolate limit for a 5 GiB part.
    const part = await upload.uploadPart(partNumber, request.body);
    return json({ partNumber: part.partNumber, etag: part.etag });
  } catch (err) {
    return json({ detail: `Part upload failed: ${errorMessage(err)}` }, 400);
  }
}

export async function handleMultipartComplete(request: Request, env: Env): Promise<Response> {
  const denied = await requireAuth(request, env);
  if (denied) return denied;

  let body: { key?: string; uploadId?: string; bucket?: string; parts?: UploadedPart[] };
  try {
    body = await request.json();
  } catch {
    return json({ detail: "Body must be JSON" }, 400);
  }

  const { key, uploadId, parts } = body;
  const bucketName = body.bucket ?? "raw-video";
  if (!key || !uploadId || !Array.isArray(parts) || parts.length === 0) {
    return json({ detail: "key, uploadId and a non-empty parts[] are required" }, 400);
  }
  if (!isValidKey(key)) return json({ detail: "Invalid storage key" }, 400);

  const bucket = resolveBucket(env, bucketName);
  if (!bucket) return json({ detail: `Bucket ${bucketName} is not uploadable` }, 400);

  // R2 requires parts in ascending order. Clients upload concurrently, so they
  // routinely finish out of order -- sort rather than reject.
  const ordered = [...parts].sort((a, b) => a.partNumber - b.partNumber);
  for (const part of ordered) {
    if (!Number.isSafeInteger(part?.partNumber) || typeof part?.etag !== "string") {
      return json({ detail: "Each part needs a numeric partNumber and a string etag" }, 400);
    }
  }

  const upload = bucket.resumeMultipartUpload(key, uploadId);
  try {
    const object = await upload.complete(ordered);
    return json({
      key: object.key,
      bucket: bucketName,
      storageUri: `s3://${bucketName}/${object.key}`,
      size: object.size,
      etag: object.httpEtag,
    });
  } catch (err) {
    return json({ detail: `Completion failed: ${errorMessage(err)}` }, 400);
  }
}

export async function handleMultipartAbort(request: Request, env: Env): Promise<Response> {
  const denied = await requireAuth(request, env);
  if (denied) return denied;

  const url = new URL(request.url);
  const key = url.searchParams.get("key");
  const uploadId = url.searchParams.get("uploadId");
  const bucketName = url.searchParams.get("bucket") ?? "raw-video";

  if (!key || !uploadId) return json({ detail: "key and uploadId are required" }, 400);
  if (!isValidKey(key)) return json({ detail: "Invalid storage key" }, 400);

  const bucket = resolveBucket(env, bucketName);
  if (!bucket) return json({ detail: `Bucket ${bucketName} is not uploadable` }, 400);

  const upload = bucket.resumeMultipartUpload(key, uploadId);
  try {
    await upload.abort();
  } catch (err) {
    // An already-aborted or already-completed upload is not an error worth
    // surfacing -- the caller's intent (this upload is gone) already holds.
    return json({ detail: `Abort failed: ${errorMessage(err)}` }, 400);
  }
  return new Response(null, { status: 204 });
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
