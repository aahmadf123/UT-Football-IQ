/**
 * CORS for a browser client on a different origin (Vercel) from the API.
 *
 * Origins are matched exactly against an allowlist, with an optional regex for
 * per-deploy preview URLs. There is deliberately no `*` fallback: the API is
 * credentialed, and `Access-Control-Allow-Origin: *` cannot carry credentials
 * anyway -- a wildcard here would look permissive while silently breaking auth.
 */

import type { Env } from "./env";

const ALLOWED_METHODS = "GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS";
const ALLOWED_HEADERS = "Authorization,Content-Type,Range,X-Requested-With";
/** Headers a browser may read off the response. Range playback needs these. */
const EXPOSED_HEADERS = "Content-Length,Content-Range,Accept-Ranges,ETag";

function allowlist(env: Env): string[] {
  return (env.CORS_ORIGINS ?? "")
    .split(",")
    .map((o) => o.trim())
    .filter(Boolean);
}

/** Resolve the echo-back origin for a request, or null if it is not allowed. */
export function resolveOrigin(request: Request, env: Env): string | null {
  const origin = request.headers.get("Origin");
  if (!origin) return null;

  if (allowlist(env).includes(origin)) return origin;

  const pattern = env.CORS_ORIGIN_REGEX?.trim();
  if (pattern) {
    try {
      // Anchored so a pattern like `https://foo\.vercel\.app` cannot be
      // satisfied by `https://evil.com/?x=https://foo.vercel.app`.
      if (new RegExp(`^(?:${pattern})$`).test(origin)) return origin;
    } catch {
      // A malformed regex must not take the API down; treat it as no match.
    }
  }
  return null;
}

/** Apply CORS headers to a response in place, returning it for chaining. */
export function withCors(response: Response, request: Request, env: Env): Response {
  const origin = resolveOrigin(request, env);
  if (!origin) return response;

  // Response headers are immutable on some constructed responses, so clone.
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", origin);
  headers.set("Access-Control-Allow-Credentials", "true");
  headers.set("Access-Control-Expose-Headers", EXPOSED_HEADERS);
  // The response varies by Origin, so shared caches must key on it.
  headers.append("Vary", "Origin");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

/** Answer a CORS preflight. Returns null when the request is not a preflight. */
export function handlePreflight(request: Request, env: Env): Response | null {
  if (request.method !== "OPTIONS") return null;
  if (!request.headers.get("Access-Control-Request-Method")) return null;

  const origin = resolveOrigin(request, env);
  if (!origin) return new Response(null, { status: 403 });

  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Credentials": "true",
      "Access-Control-Allow-Methods": ALLOWED_METHODS,
      "Access-Control-Allow-Headers": ALLOWED_HEADERS,
      "Access-Control-Max-Age": "86400",
      Vary: "Origin",
    },
  });
}
