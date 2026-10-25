import { expect, test } from "@playwright/test";
import { mockBackend, sampleVideo, type Route } from "./helpers";

// Synthetic catalog/transport regression only. No real recordings, credentials,
// backend database or staff annotations are used by this browser flow.
test("session attachment drains the full catalog despite persisted date/type filters", async ({ page }) => {
  const sessionId = "attach-session";
  const catalog = Array.from({ length: 205 }, (_, index) => sampleVideo({
    id: `attach-video-${index}`,
    filename: `protocol-recording-${index}.mp4`,
    practice_session_id: index === 0 ? sessionId : index === 1 ? "other-session" : null,
    session_kind: index === 0 ? "game" : "practice",
    recorded_at: index === 0 ? "2026-01-01T10:00:00Z" : "2025-10-01T10:00:00Z",
  }));
  const last = catalog.at(-1)!;
  let attached = false;
  const videoQueries: URLSearchParams[] = [];
  const posted: unknown[] = [];
  const session = () => ({
    id: sessionId,
    title: "Protocol grouped camera session",
    session_kind: "game",
    recorded_at: "2026-01-01T10:00:00Z",
    created_at: "2026-01-01T10:00:00Z",
    updated_at: "2026-01-01T10:00:00Z",
    library_role: "toledo",
    opponent_team: "Protocol opponent",
    notes: null,
    recording_count: attached ? 2 : 1,
    play_count: 0,
    source_window_count: 0,
    recordings: attached ? [catalog[0], { ...last, practice_session_id: sessionId, camera_label: "Protocol end zone", session_kind: "game" }] : [catalog[0]],
    plays: [],
  });

  await mockBackend(page, {
    "GET /api/v1/videos": (route: Route) => {
      const query = new URL(route.request().url()).searchParams;
      videoQueries.push(query);
      // The shared dashboard has only one match. Session attachment must use
      // its own unfiltered catalog and request the second page for the last row.
      if (query.has("session_kind") || query.has("recorded_after")) return catalog.slice(0, 1);
      const offset = Number(query.get("offset") ?? 0);
      const limit = Number(query.get("limit") ?? 50);
      return catalog.slice(offset, offset + limit).map((video) => attached && video.id === last.id ? { ...video, practice_session_id: sessionId } : video);
    },
    "GET /api/v1/videos/*": [],
    "GET /api/v1/sessions": () => [session()],
    [`GET /api/v1/sessions/${sessionId}`]: () => session(),
    [`POST /api/v1/sessions/${sessionId}/recordings`]: (route: Route) => {
      posted.push(route.request().postDataJSON());
      attached = true;
      return session();
    },
    "GET /api/v1/jobs": [],
    "GET /api/v1/inbox/status": [],
    "GET /api/v1/players": [],
    "GET /api/v1/players/metrics/summary": [],
  });
  await page.addInitScript(() => localStorage.setItem("football_iq_app_state_v1", JSON.stringify({ sessionType: "game", selectedDate: "2026-01-01" })));
  await page.goto(`/film-room/?tab=browse&sessionId=${sessionId}`);
  await expect(page.getByRole("heading", { name: "Protocol grouped camera session" })).toBeVisible();
  await page.getByText("Group a recording already in your library", { exact: true }).click();
  const form = page.locator("details").filter({ has: page.getByText("Group a recording already in your library", { exact: true }) });
  const picker = form.getByLabel("Recording", { exact: true });
  await expect(form.getByText("205 uploaded · 203 available to add", { exact: true })).toBeVisible();
  await expect(form.getByText(/Already in this session: 1\. Grouped in other sessions: 1\./)).toBeVisible();
  await expect(picker.locator("option")).toHaveCount(204); // placeholder + eligible rows
  await expect(picker.locator('option[value="attach-video-0"]')).toHaveCount(0);
  await expect(picker.locator('option[value="attach-video-1"]')).toHaveCount(0);
  await expect(picker.locator(`option[value="${last.id}"]`)).toHaveCount(1);
  await expect.poll(() => videoQueries.some((query) => query.get("session_kind") === "game" && query.has("recorded_after"))).toBe(true);
  expect(videoQueries.some((query) => query.get("offset") === "200" && query.get("limit") === "200" && !query.has("session_kind") && !query.has("recorded_after") && !query.has("practice_session_id"))).toBe(true);

  await picker.selectOption(last.id);
  await form.getByLabel("Camera name").fill("  Protocol end zone  ");
  const request = page.waitForRequest((req) => req.method() === "POST" && new URL(req.url()).pathname === `/api/v1/sessions/${sessionId}/recordings`);
  await form.getByRole("button", { name: "Add to session", exact: true }).click();
  expect((await request).postDataJSON()).toEqual({ video_id: last.id, camera_label: "Protocol end zone" });
  await expect.poll(() => posted).toEqual([{ video_id: last.id, camera_label: "Protocol end zone" }]);
  await expect(page.getByText(last.filename, { exact: true })).toBeVisible();
  await expect(page.getByText("Protocol end zone · 2025-10-01", { exact: true })).toBeVisible();
  await expect(form.getByText("205 uploaded · 202 available to add", { exact: true })).toBeVisible();
  await expect(form.getByText(/Already in this session: 2\. Grouped in other sessions: 1\./)).toBeVisible();
  await expect(picker.locator(`option[value="${last.id}"]`)).toHaveCount(0);
  await expect(picker).toHaveValue("");
  await expect(form.getByLabel("Camera name")).toHaveValue("");
});

