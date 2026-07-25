/**
 * Resolve the backend origin.
 *
 * `NEXT_PUBLIC_API_URL` is embedded at build time (this app is a static
 * export), so a production bundle must be built with the deployed backend's
 * origin set. There is no baked-in fallback host: shipping one would silently
 * point every deployment at somebody else's API.
 *
 * An empty result means "same origin / relative fetch", which is what
 * development and test builds want.
 */
export function apiBase(): string {
  return (process.env.NEXT_PUBLIC_API_URL ?? "").trim().replace(/\/+$/, "");
}
