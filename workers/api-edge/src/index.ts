/**
 * Football-IQ edge Worker.
 *
 * Sits between the Vercel frontend and the FastAPI container. It handles the
 * three things that are genuinely better at the edge and proxies everything
 * else through untouched:
 *
 *   1. R2 multipart uploads   -- bytes never route through the container
 *   2. Signed object streaming -- Range-correct playback straight from R2
 *   3. Cron -> nightly tick    -- the in-process scheduler cannot survive a
 *                                 container that sleeps
 *
 * Design rule: the Worker is not an authorization boundary. It verifies
 * signatures to keep unauthenticated traffic from waking the container and
 * spending R2 writes, but the container re-verifies every token and applies the
 * RBAC policy matrix. Nothing here should ever be the *only* check.
 */

import type { Env } from "./env";
import { handlePreflight, withCors } from "./cors";
import {
  handleMultipartAbort,
  handleMultipartComplete,
  handleMultipartCreate,
  handleMultipartPart,
} from "./multipart";
import { proxyToBackend } from "./proxy";
import { handleStorageGet, json } from "./storage";

export { BackendContainer } from "./container";

const STORAGE_PREFIX = "/api/v1/storage/";
const MULTIPART_PREFIX = "/api/v1/uploads/multipart/";

export async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const { pathname } = url;

  // Liveness for the edge itself, answerable while the container sleeps.
  if (pathname === "/edge/health") {
    return json({ status: "ok", environment: env.ENVIRONMENT ?? "unknown" });
  }

  if (pathname.startsWith(MULTIPART_PREFIX)) {
    const action = pathname.slice(MULTIPART_PREFIX.length);
    if (action === "create" && request.method === "POST") {
      return handleMultipartCreate(request, env);
    }
    if (action === "part" && request.method === "PUT") {
      return handleMultipartPart(request, env);
    }
    if (action === "complete" && request.method === "POST") {
      return handleMultipartComplete(request, env);
    }
    if (action === "abort" && request.method === "DELETE") {
      return handleMultipartAbort(request, env);
    }
    return json({ detail: "Method not allowed" }, 405);
  }

  if (pathname.startsWith(STORAGE_PREFIX)) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ detail: "Method not allowed" }, 405);
    }
    // `/api/v1/storage/{bucket}/{key...}` -- the key may contain slashes, and
    // each segment is percent-encoded by the signer, so decode per segment.
    const rest = pathname.slice(STORAGE_PREFIX.length);
    const slash = rest.indexOf("/");
    if (slash <= 0 || slash === rest.length - 1) {
      return json({ detail: "Not found" }, 404);
    }
    let bucket: string;
    let key: string;
    try {
      bucket = decodeURIComponent(rest.slice(0, slash));
      key = rest
        .slice(slash + 1)
        .split("/")
        .map(decodeURIComponent)
        .join("/");
    } catch {
      // decodeURIComponent throws on a malformed escape like "%zz".
      return json({ detail: "Malformed storage path" }, 400);
    }
    return handleStorageGet(request, env, bucket, key);
  }

  return proxyToBackend(request, env);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const preflight = handlePreflight(request, env);
    if (preflight) return preflight;

    try {
      const response = await route(request, env);
      return withCors(response, request, env);
    } catch (err) {
      // Never leak a stack trace to the client; log it for `wrangler tail`.
      console.error(
        JSON.stringify({
          event: "edge_unhandled_error",
          path: new URL(request.url).pathname,
          error: err instanceof Error ? err.message : String(err),
        }),
      );
      return withCors(json({ detail: "Internal error" }, 500), request, env);
    }
  },

  /**
   * Nightly tick.
   *
   * The backend used to run this as an asyncio loop on the FastAPI lifespan,
   * which only works while a process is alive -- a container that sleeps after
   * 20 minutes would simply never reach 08:00 UTC. Cron moves the trigger
   * outside the container. The backend still takes its Postgres advisory lock,
   * so a duplicate delivery is harmless.
   */
  async scheduled(event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      (async () => {
        const container = env.BACKEND.getByName("primary");
        try {
          const response = await container.fetch(
            new Request("http://backend/internal/scheduler/tick", {
              method: "POST",
              headers: {
                Authorization: `Bearer ${env.SCHEDULER_TOKEN}`,
                "Content-Type": "application/json",
              },
            }),
          );
          console.log(
            JSON.stringify({
              event: "scheduler_tick",
              cron: event.cron,
              status: response.status,
              body: response.ok ? await response.text() : undefined,
            }),
          );
        } catch (err) {
          console.error(
            JSON.stringify({
              event: "scheduler_tick_failed",
              cron: event.cron,
              error: err instanceof Error ? err.message : String(err),
            }),
          );
        }
      })(),
    );
  },
};
