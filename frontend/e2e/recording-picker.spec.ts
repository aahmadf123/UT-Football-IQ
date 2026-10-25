import { expect, test } from "@playwright/test";
import { mockBackend, sampleClip, sampleVideo, type Route } from "./helpers";

// Protocol catalog fixtures exercise visibility/navigation, not real uploads or
// the production inventory. The persisted dashboard list deliberately has one.
test("recording picker exposes session scope, clears it and follows URL navigation", async ({ page }) => {
  const videos = Array.from({ length: 30 }, (_, index) => sampleVideo({
    id: `drone-${index}`, filename: `practice-drone-${index}.mp4`,
    practice_session_id: index === 0 ? "session-one" : "session-two",
  }));
  const requestedClips: string[] = [];
  await mockBackend(page, {
    "GET /api/v1/videos": (route: Route) => {
      const query = new URL(route.request().url()).searchParams;
      if (query.has("session_kind") || query.has("recorded_after")) return videos.slice(0, 1);
      const scoped = query.get("practice_session_id");
      const rows = scoped ? videos.filter((video) => video.practice_session_id === scoped) : videos;
      return rows.slice(Number(query.get("offset") ?? 0), Number(query.get("offset") ?? 0) + Number(query.get("limit") ?? 50));
    },
    "GET /api/v1/videos/*": (route: Route) => {
      const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
      requestedClips.push(id);
      return [sampleClip({ id: `clip-${id}`, video_id: id })];
    },
    "GET /api/v1/jobs": [], "GET /api/v1/inbox/status": [],
    "GET /api/v1/players": [], "GET /api/v1/players/metrics/summary": [],
  });
  await page.addInitScript(() => localStorage.setItem("football_iq_app_state_v1", JSON.stringify({ sessionType: "game", selectedDate: "2026-01-01" })));
  await page.goto("/film-room/?tab=review&sessionId=session-one&videoId=drone-0");
  await expect(page.getByTestId("recording-scope")).toContainText("1 recording in the selected session");
  await expect(page.getByTestId("review-video-picker").locator("option")).toHaveCount(1);
  await page.getByRole("button", { name: "Show all recordings" }).click();
  await expect(page).not.toHaveURL(/sessionId=|videoId=/);
  await expect(page.getByTestId("review-video-picker").locator("option")).toHaveCount(30);
  await expect(page.getByTestId("recording-scope")).toContainText("30 recordings · All uploaded recordings");
  await page.getByTestId("review-video-picker").selectOption("drone-29");
  await expect(page.getByTestId("review-clip-clip-drone-29")).toBeVisible();
  expect(requestedClips).toContain("drone-29");

  await page.evaluate(() => history.pushState(null, "", "/film-room/?tab=review&sessionId=session-two&videoId=drone-2"));
  await expect(page.getByTestId("recording-scope")).toContainText("29 recordings in the selected session");
  await expect(page.getByTestId("review-video-picker")).toHaveValue("drone-2");
  await expect(page.getByTestId("review-clip-clip-drone-2")).toBeVisible();
  await expect(page.getByTestId("review-clip-clip-drone-29")).toHaveCount(0);
  await page.goBack();
  await expect(page.getByTestId("recording-scope")).toContainText("30 recordings · All uploaded recordings");
});

test("unknown recording links stay explicit and refresh discovers newly uploaded rows", async ({ page }) => {
  let rows = [sampleVideo({ id: "one", filename: "one.mp4" })];
  const clips: string[] = [];
  await mockBackend(page, {
    "GET /api/v1/videos": () => rows,
    "GET /api/v1/videos/*": (route: Route) => {
      const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
      clips.push(id); return [sampleClip({ id: `clip-${id}`, video_id: id })];
    },
    "GET /api/v1/jobs": [], "GET /api/v1/inbox/status": [],
    "GET /api/v1/players": [], "GET /api/v1/players/metrics/summary": [],
  });
  await page.goto("/film-room/?tab=review&videoId=missing");
  await expect(page.getByTestId("review-empty")).toContainText("requested recording is not available");
  expect(clips).toEqual([]);
  await expect(page.getByText(/Only film uploaded to this library/)).toBeVisible();
  rows = [...rows, sampleVideo({ id: "missing", filename: "newly-uploaded.mp4" })];
  await page.getByRole("button", { name: "Refresh recordings" }).click();
  await expect(page.getByTestId("review-video-picker")).toHaveValue("missing");
  await expect(page.getByTestId("review-clip-clip-missing")).toBeVisible();
});
