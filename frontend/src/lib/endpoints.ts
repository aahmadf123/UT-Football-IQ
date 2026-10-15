/**
 * Resolve the backend origin.
 *
 * A local install uses same-origin requests. Hosted deployments can provide
 * a runtime or build-time origin; coaches can explicitly connect a team API.
 * There is no baked-in hosted API address.
 */
declare global {
  interface Window {
    __FOOTBALL_IQ_CONFIG__?: { apiUrl?: string };
  }
}

/** A local install serves the UI and API together; an origin override is optional. */
export function apiBase(): string {
  const runtime = typeof window === "undefined" ? undefined : window.__FOOTBALL_IQ_CONFIG__?.apiUrl;
  let saved: string | null = null;
  if (typeof window !== "undefined") {
    try { saved = window.localStorage.getItem("football-iq-api-origin"); } catch { /* storage is optional */ }
  }
  return (saved ?? runtime ?? process.env.NEXT_PUBLIC_API_URL ?? "").trim().replace(/\/+$/, "");
}

/** Explicitly switching libraries clears the old session before any request. */
export function configureApiUrl(value: string): void {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (trimmed) {
    const parsed = new URL(trimmed);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error("Enter an HTTP or HTTPS API address without credentials or query parameters.");
    }
  }
  window.localStorage.setItem("football-iq-api-origin", trimmed);
  window.localStorage.removeItem("football-iq-auth-v1");
  window.localStorage.removeItem("football_iq_app_state_v1");
}
