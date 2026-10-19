/**
 * Forward a request to the FastAPI container.
 *
 * Everything the edge does not handle itself goes here unchanged. The container
 * re-verifies the JWT and owns all authorization, so the proxy adds no trust --
 * it only carries the client's real IP and protocol forward so the backend's
 * `_upload_base()` can mint absolute URLs that point back at the public origin
 * rather than at localhost.
 */

import type { Env } from "./env";

/**
 * All traffic targets one container instance.
 *
 * `alerts_sse.py` keeps its subscriber map in process memory, so fanning
 * requests across instances would silently drop alert streams for anyone whose
 * POST landed on a different instance than their `GET /alerts/stream`. One
 * instance is the honest configuration until that map moves to a Durable
 * Object; scaling out is a deliberate change, not an accident of routing.
 */
const INSTANCE_NAME = "primary";

/** Hop-by-hop headers that must not be forwarded (RFC 9110 §7.6.1). */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

export async function proxyToBackend(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);

  const headers = new Headers();
  for (const [name, value] of request.headers) {
    if (!HOP_BY_HOP.has(name.toLowerCase())) headers.set(name, value);
  }

  // The backend builds absolute upload URLs from these when
  // PUBLIC_API_BASE_URL still points at localhost.
  headers.set("X-Forwarded-Proto", url.protocol.replace(":", ""));
  headers.set("X-Forwarded-Host", url.host);
  const clientIp = request.headers.get("CF-Connecting-IP");
  if (clientIp) headers.set("X-Forwarded-For", clientIp);

  // Only bodiless reads are replayed: a POST that failed mid-flight may already
  // have been applied, and its body stream can't be read twice anyway.
  const replayable = REPLAYABLE.has(request.method);
  for (let attempt = 0; ; attempt++) {
    let failure: string;
    try {
      const container = env.BACKEND.getByName(INSTANCE_NAME);
      const response = await container.fetch(
        new Request(url, { method: request.method, headers, body: request.body }),
      );
      const unavailable = await containerUnavailable(response);
      if (unavailable === null) return response;
      failure = unavailable;
    } catch (err) {
      // The Durable Object itself is reset while a new version rolls out.
      failure = err instanceof Error ? err.message : String(err);
    }
    if (!replayable || attempt >= RETRY_DELAYS_MS.length) {
      console.error(
        JSON.stringify({ event: "container_unavailable", path: url.pathname, attempts: attempt + 1, failure }),
      );
      return restarting();
    }
    await sleep(RETRY_DELAYS_MS[attempt]);
  }
}

const REPLAYABLE = new Set(["GET", "HEAD"]);
/** Backoff between attempts; a rollout's cold start takes a few seconds. */
export const RETRY_DELAYS_MS = [500, 1500, 3000];

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Bodies the containers library writes when the backend never answered. */
const UNAVAILABLE_PREFIXES = [
  "There is no Container instance",
  "Failed to start container",
  "Container suddenly disconnected",
  "Error proxying request to container",
];

/**
 * The library's own failure text when the container is starting, restarting
 * or dropped the connection, else ``null``. Only the library's plain-text
 * 500/503s are inspected (peeking a clone), so the backend's own errors pass
 * through untouched.
 */
export async function containerUnavailable(response: Response): Promise<string | null> {
  if (![500, 503].includes(response.status)) return null;
  if ((response.headers.get("Content-Type") ?? "").includes("application/json")) return null;
  const text = await response.clone().text();
  return UNAVAILABLE_PREFIXES.some((prefix) => text.startsWith(prefix))
    ? `${response.status} ${text.slice(0, 120)}`
    : null;
}

/** What a browser sees when the backend stays unreachable: a retryable 503. */
function restarting(): Response {
  return new Response(
    JSON.stringify({ detail: "The server is restarting. Try again in a moment." }),
    { status: 503, headers: { "Content-Type": "application/json", "Retry-After": "5" } },
  );
}
