/**
 * Football-IQ Cloudflare Worker — edge API entrypoint.
 *
 * Routes:
 *   GET  /health                          -> liveness probe (no auth)
 *   POST /api/v1/videos/upload-url        -> Worker proxy upload URL + R2 key
 *   PUT  /api/v1/videos/upload/*          -> proxy PUT to R2 raw-video bucket
 *   GET  /api/v1/videos/download-url      -> HMAC-signed R2 streaming URL
 *   POST /api/v1/jobs                     -> submit video processing job
 *   GET  /dl/:bucket/*key                 -> stream R2 object (HMAC-verified)
 *
 * All /api/* routes require a valid Bearer JWT issued by the backend.
 * The /dl/* streaming route uses HMAC-signed query params instead.
 */

import { extractBearerToken, verifyJwt } from "./auth.js";
import {
  buildUploadProxyUrl,
  createSignedStreamUrl,
  getObject,
  isDownloadableBucket,
  isValidKey,
  putObject,
  verifySignedUrl,
} from "./r2.js";
import type { Env } from "./types.js";

// ── Response helpers ──────────────────────────────────────────────────────────

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function corsHeaders(origin: string): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Range",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
    "Access-Control-Max-Age": "86400",
  };
}

function withCors(response: Response, cors: Record<string, string>): Response {
  const headers = new Headers(response.headers);
  for (const [k, v] of Object.entries(cors)) {
    headers.set(k, v);
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

// ── Router ───────────────────────────────────────────────────────────────────

export default {
  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") ?? "*";
    const cors = corsHeaders(origin);

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    // ── Liveness probe ────────────────────────────────────────────────────
    if (url.pathname === "/health" && request.method === "GET") {
      return withCors(json({ status: "ok" }), cors);
    }

    // ── Signed streaming endpoint (no Bearer auth — HMAC-verified) ────────
    if (url.pathname.startsWith("/dl/") && request.method === "GET") {
      return withCors(await handleStream(request, env, url), cors);
    }

    // ── Auth gate for all /api/* routes ───────────────────────────────────
    if (url.pathname.startsWith("/api/")) {
      const token = extractBearerToken(request);
      if (!token) {
        return withCors(json({ error: "Missing authorization header" }, 401), cors);
      }
      try {
        await verifyJwt(token, env.JWT_SECRET);
      } catch (err) {
        if (err instanceof Response) return withCors(err, cors);
        return withCors(json({ error: "Authentication failed" }, 401), cors);
      }
    }

    // ── POST /api/v1/videos/upload-url ────────────────────────────────────
    if (url.pathname === "/api/v1/videos/upload-url" && request.method === "POST") {
      const body = (await request.json()) as { filename: string };
      if (!body.filename) {
        return withCors(json({ error: "filename is required" }, 400), cors);
      }
      const key = `raw/${Date.now()}-${body.filename}`;
      const uploadUrl = buildUploadProxyUrl(request.url, key);
      return withCors(json({ uploadUrl, key }), cors);
    }

    // ── PUT /api/v1/videos/upload/* ───────────────────────────────────────
    const uploadMatch = url.pathname.match(/^\/api\/v1\/videos\/upload\/(.+)$/);
    if (uploadMatch && request.method === "PUT") {
      const key = decodeURIComponent(uploadMatch[1]);
      if (!key) {
        return withCors(json({ error: "Missing upload key" }, 400), cors);
      }
      try {
        const contentType = request.headers.get("Content-Type") ?? "video/mp4";
        const obj = await putObject(env, "raw-video", key, request.body, contentType);
        return withCors(json({
          key,
          size: obj.size,
          etag: obj.etag,
          storageUri: `r2://raw-video/${key}`,
        }, 201), cors);
      } catch (err) {
        const message = err instanceof Error ? err.message : "Upload failed";
        return withCors(json({ error: message }, 500), cors);
      }
    }

    // ── GET /api/v1/videos/download-url ───────────────────────────────────
    if (url.pathname === "/api/v1/videos/download-url" && request.method === "GET") {
      const bucket = url.searchParams.get("bucket");
      const key = url.searchParams.get("key");

      if (!bucket || !key) {
        return withCors(json({ error: "bucket and key query params are required" }, 400), cors);
      }
      if (!isDownloadableBucket(bucket)) {
        return withCors(json({ error: `Bucket not allowed: ${bucket}` }, 400), cors);
      }
      if (!isValidKey(key)) {
        return withCors(json({ error: "Invalid key" }, 400), cors);
      }

      try {
        const downloadUrl = await createSignedStreamUrl(env, request.url, bucket, key);
        return withCors(json({ downloadUrl }), cors);
      } catch (err) {
        const message = err instanceof Error ? err.message : "Could not generate download URL";
        return withCors(json({ error: message }, 500), cors);
      }
    }

    // NOTE: job dispatch no longer lives at the edge. The backend's
    // processing_jobs table IS the queue (POST /api/v1/jobs on the backend,
    // claimed by the GPU worker via /jobs/claim), and the nightly
    // workload-rollup is enqueued by the backend scheduler. The Worker's
    // former POST /api/v1/jobs route and cron handler were removed with
    // that migration — this Worker is now purely the upload/stream edge.

    return withCors(json({ error: "Not found" }, 404), cors);
  },
};

// ── Stream handler ────────────────────────────────────────────────────────────

async function handleStream(request: Request, env: Env, url: URL): Promise<Response> {
  // Parse /dl/<bucket>/<key...>
  const pathAfterDl = url.pathname.slice(4); // strip "/dl/"
  const slashIdx = pathAfterDl.indexOf("/");
  if (slashIdx === -1) {
    return json({ error: "Invalid stream path" }, 400);
  }

  const bucket = decodeURIComponent(pathAfterDl.slice(0, slashIdx));
  const key = decodeURIComponent(pathAfterDl.slice(slashIdx + 1));

  if (!isDownloadableBucket(bucket)) {
    return json({ error: `Bucket not allowed: ${bucket}` }, 403);
  }
  if (!isValidKey(key)) {
    return json({ error: "Invalid key" }, 400);
  }

  const exp = url.searchParams.get("exp");
  const sig = url.searchParams.get("sig");
  if (!exp || !sig) {
    return json({ error: "Missing signature parameters" }, 401);
  }

  const valid = await verifySignedUrl(env, bucket, key, exp, sig);
  if (!valid) {
    return json({ error: "Invalid or expired signature" }, 403);
  }

  const rangeHeader = request.headers.get("Range");
  const obj = await getObject(env, bucket, key, rangeHeader);
  if (!obj) {
    return json({ error: "Object not found" }, 404);
  }

  const headers = new Headers();
  headers.set("Content-Type", obj.httpMetadata?.contentType ?? "video/mp4");
  headers.set("Accept-Ranges", "bytes");
  headers.set("Cache-Control", "private, max-age=3600");
  headers.set("X-Content-Type-Options", "nosniff");

  if (obj.size !== undefined) {
    if (rangeHeader && obj.range) {
      const range = obj.range as { offset: number; length?: number };
      const start = range.offset;
      const length = range.length ?? (obj.size - start);
      if (length <= 0) {
        return json({ error: "Invalid range" }, 416);
      }
      const end = start + length - 1;
      headers.set("Content-Length", String(length));
      headers.set("Content-Range", `bytes ${start}-${end}/${obj.size}`);
      return new Response(obj.body, { status: 206, headers });
    }
    headers.set("Content-Length", String(obj.size));
  }

  return new Response(obj.body, { status: 200, headers });
}
