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

  const container = env.BACKEND.getByName(INSTANCE_NAME);
  return container.fetch(new Request(url, { method: request.method, headers, body: request.body }));
}
