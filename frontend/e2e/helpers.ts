/**
 * Shared helpers for the Football-IQ E2E suite.
 *
 * The suite intercepts every request to the fake API/Worker hosts declared
 * in `playwright.config.ts`. Helpers here keep individual specs small and
 * make it obvious which backend endpoints each test exercises.
 */
import type { Page, Route } from "@playwright/test";
import { FAKE_API_URL, FAKE_WORKER_URL } from "../playwright.config";

export type { Route };
export { FAKE_API_URL, FAKE_WORKER_URL };

export type JsonHandler = (route: Route) => unknown | Promise<unknown>;

interface RouteMap {
  // Map of `METHOD path` (path may include a trailing wildcard) to a handler
  // that returns the JSON body, or a function for custom routing.
  [route: string]: unknown | JsonHandler;
}

/** Build a decodable (unsigned) JWT so roles.ts / auth.tsx can read the role. */
export function makeE2eJwt(role = "coach"): string {
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64");
  const payload = Buffer.from(
    JSON.stringify({ sub: "e2e-user", role, type: "access" }),
  ).toString("base64");
  return `${header}.${payload}.e2e-signature`;
}

/**
 * Seed a signed-in session before the app boots. The shell requires a
 * session whenever a backend is configured (which the e2e suite always
 * fakes), so every spec runs as an authenticated coach by default.
 */
export async function seedAuthSession(page: Page, role = "coach"): Promise<void> {
  const token = makeE2eJwt(role);
  await page.addInitScript(
    ([storageValue]) => {
      window.localStorage.setItem("football-iq-auth-v1", storageValue);
    },
    [
      JSON.stringify({
        token,
        refreshToken: "e2e-refresh-token",
        user: { email: "coach@e2e.local", role },
      }),
    ],
  );
}

/**
 * Register JSON responders for backend endpoints. Any unmatched request to
 * the fake API host returns 404 so unintended calls fail visibly.
 *
 * Also seeds an authenticated coach session and a default
 * `/api/v1/auth/refresh` responder (override by passing your own).
 */
export async function mockBackend(page: Page, routes: RouteMap): Promise<void> {
  await seedAuthSession(page);
  const withAuthDefaults: RouteMap = {
    "POST /api/v1/auth/refresh": {
      access_token: makeE2eJwt("coach"),
      refresh_token: "e2e-refresh-token",
    },
    ...routes,
  };
  await mockHost(page, "api.e2e.local", withAuthDefaults);
}

/**
 * Same as `mockBackend` but does **not** pre-seed an auth session in
 * localStorage. Use this for specs that need to exercise the login page
 * (where a pre-existing token would immediately redirect away from it).
 *
 * Auth-endpoint responders (login, register, refresh) are injected by the
 * caller so each test controls the happy- or error-path outcome.
 */
export async function mockBackendNoAuth(page: Page, routes: RouteMap): Promise<void> {
  await mockHost(page, "api.e2e.local", routes);
}

/**
 * Register responders for the Cloudflare Worker host (upload-url + signed
 * download URLs + the simulated PUT to R2).
 */
export async function mockWorker(page: Page, routes: RouteMap): Promise<void> {
  await mockHost(page, "worker.e2e.local", routes);
}

async function mockHost(page: Page, hostname: string, routes: RouteMap): Promise<void> {
  await page.route(
    (url) => url.hostname === hostname,
    async (route) => {
      const req = route.request();
      const corsHeaders = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
      };

      if (req.method() === "OPTIONS") {
        await route.fulfill({
          status: 204,
          headers: corsHeaders,
        });
        return;
      }

      const url = new URL(req.url());
      const key = `${req.method()} ${url.pathname}`;
      const matched = matchRoute(routes, key);
      if (matched === undefined) {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          headers: corsHeaders,
          body: JSON.stringify({ detail: `unmocked ${key}` }),
        });
        return;
      }
      if (typeof matched === "function") {
        const result = await (matched as JsonHandler)(route);
        if (result === undefined) return; // handler called route.fulfill itself
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: corsHeaders,
          body: JSON.stringify(result),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: corsHeaders,
        body: JSON.stringify(matched),
      });
    },
  );
}

function matchRoute(routes: RouteMap, key: string): unknown | undefined {
  if (key in routes) return routes[key];
  // Allow trailing `*` wildcard matching for nested IDs.
  for (const pattern of Object.keys(routes)) {
    if (pattern.endsWith("*") && key.startsWith(pattern.slice(0, -1))) {
      return routes[pattern];
    }
  }
  return undefined;
}

/** Sample video payload matching the backend's ApiVideo schema. */
export function sampleVideo(overrides: Record<string, unknown> = {}) {
  return {
    id: "v-1",
    filename: "PR_20251001_DRONEA.mp4",
    status: "ready",
    duration_seconds: 120,
    fps: 30,
    width: 1920,
    height: 1080,
    created_at: "2025-10-01T10:00:00Z",
    recorded_at: "2025-10-01T09:30:00Z",
    session_kind: "practice",
    source_type: "drone",
    opponent_team: null,
    practice_session_id: "ps-1",
    our_possession: "offense",
    storage_uri: "r2://raw-video/raw/v-1",
    ...overrides,
  };
}

/** Sample clip payload matching the backend's ApiClip schema. */
export function sampleClip(overrides: Record<string, unknown> = {}) {
  return {
    id: "c-1",
    video_id: "v-1",
    start_time: 12.0,
    end_time: 18.5,
    play_number: 1,
    storage_uri: "r2://clips/clips/c-1.mp4",
    confidence: 0.92,
    is_reviewed: false,
    our_possession: "offense",
    side_of_ball: "offense",
    session_kind: "practice",
    ...overrides,
  };
}

/**
 * Sample clip-overlays payload matching the backend's ClipOverlayResponse
 * schema, including the calibration + capture-regime fields (explained
 * suppression). Defaults to an analytics-safe, empty-layers payload.
 */
export function sampleOverlays(overrides: Record<string, unknown> = {}) {
  return {
    clip_id: "c-1",
    capture_regime: "drone_follow",
    tracklets: [],
    events: [],
    labels: [],
    metrics: [],
    layers_available: { tracklets: false, events: false, labels: false, metrics: false },
    calibration: { analytics_safe: true, reason: null, reason_codes: [], confidence: 0.94 },
    ...overrides,
  };
}

/** Sample overlay tracklet for specs that need a pickable track. */
export function sampleTracklet(overrides: Record<string, unknown> = {}) {
  return {
    id: "t-1",
    player_id: null,
    start_frame: 0,
    end_frame: 300,
    track_confidence: 0.9,
    team_label: "home",
    position_group: "Skill",
    side_of_ball: "offense",
    track_points: [],
    ...overrides,
  };
}

/** Sample practice-session group. */
export function samplePracticeSession(overrides: Record<string, unknown> = {}) {
  return {
    practice_session_id: "ps-1",
    session_date: "2025-10-01",
    session_kind: "practice",
    opponent_team: null,
    video_count: 1,
    ...overrides,
  };
}

/** Sample inbox row matching the backend's VideoInboxItem schema. */
export function sampleInboxItem(overrides: Record<string, unknown> = {}) {
  return {
    video_id: "v-1",
    filename: "PR_20251001_DRONEA.mp4",
    video_status: "ready",
    total_jobs: 6,
    running_jobs: 0,
    succeeded_jobs: 6,
    failed_jobs: 0,
    clip_count: 12,
    preliminary_clip_count: 0,
    calibration_safe_pct: 94,
    latest_error_stage: null,
    latest_error_message: null,
    same_session_job_count: 2,
    pose_pipeline_active: true,
    created_at: "2025-10-01T10:00:00Z",
    ...overrides,
  };
}
