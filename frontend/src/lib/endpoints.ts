const PRODUCTION_API_URL = "https://football-iq-backend.fly.dev";

const LOCALHOST_RE = /^https?:\/\/(localhost|127\.\d+\.\d+\.\d+)(:\d+)?$/i;

/**
 * Resolve the backend origin. Production static exports need a usable default
 * because NEXT_PUBLIC_* values are embedded at build time.
 *
 * If a build accidentally embeds a localhost URL in production (the common
 * copy-from-.env.example mistake), ignore it and fall back to the real backend.
 */
export function apiBase(): string {
  const configured = (process.env.NEXT_PUBLIC_API_URL ?? "").trim().replace(/\/+$/, "");
  if (configured) {
    if (process.env.NODE_ENV === "production" && LOCALHOST_RE.test(configured)) {
      const hostname = typeof window !== "undefined" ? window.location.hostname : "";
      if (hostname !== "localhost" && hostname !== "127.0.0.1") return PRODUCTION_API_URL;
    }
    return configured;
  }
  return process.env.NODE_ENV === "production" ? PRODUCTION_API_URL : "";
}
