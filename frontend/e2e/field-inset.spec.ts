import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { readFileSync } from "node:fs";
import { mockBackend, sampleClip, sampleOverlays, sampleTracklet, sampleVideo } from "./helpers";

async function serveNativeFixture(page: Page) {
    const media = readFileSync(path.resolve(__dirname, "fixtures/field-inset-native.mp4"));
    await page.route("https://media.e2e.local/field-native.mp4", (route) => {
      const range = route.request().headers().range?.match(/^bytes=(\d+)-(\d*)$/);
      const start = range ? Number(range[1]) : 0;
      const end = range?.[2] ? Math.min(Number(range[2]), media.length - 1) : media.length - 1;
      return route.fulfill({ status: range ? 206 : 200, contentType: "video/mp4", body: media.subarray(start, end + 1),
        headers: { "Accept-Ranges": "bytes", ...(range ? { "Content-Range": `bytes ${start}-${end}/${media.length}` } : {}) },
      });
    });
}

// Real decoded 2s/30fps timing fixture, plain gray canvas. All actor coordinates
// here are synthetic UI contract data, never observations of supplied film.
for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`solid field inset controls and native playhead at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await serveNativeFixture(page);
    await mockBackend(page, {
      "GET /api/v1/*": [],
      "GET /api/v1/clips/c-1": sampleClip({ start_time: 0, end_time: 2, provenance: { playback: { kind: "clean_clip" } } }),
      "GET /api/v1/videos/v-1": sampleVideo({ filename: "SYNTHETIC_UI_TEST_NOT_GAME_FILM.mp4", width: 960, height: 540, fps: 30 }),
      "GET /api/v1/videos/download-url": { downloadUrl: "https://media.e2e.local/field-native.mp4" },
      "GET /api/v1/clips/c-1/overlays": sampleOverlays({ tracks_timebase: "recording", source_start_time_seconds: 0,
        layers_available: { tracklets: true, events: false, labels: false, metrics: false },
        tracklets: [sampleTracklet({ end_frame: 3, track_points: [0, 3].map((frame_number) => ({
          frame_number, timestamp_seconds: frame_number / 30, shot_id: "0", visibility_state: "observed",
          field_x: 20 + frame_number, field_y: 0, bbox: [300, 180, 320, 220], detection_confidence: .9,
        })) })],
      }),
    });
    await page.goto("/clip-review/?clipId=c-1");
    const video = page.locator("video");
    await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.readyState)).toBeGreaterThanOrEqual(1);
    await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.seekable.length ? el.seekable.end(0) : 0)).toBe(2);
    await video.evaluate((el: HTMLVideoElement) => el.pause());
    const inset = page.getByTestId("field-inset");
    await expect(inset.getByRole("button", { name: "Hide field view" })).toBeVisible();
    await expect(inset.locator("circle")).toHaveCount(1);
    expect(await inset.evaluate((el) => ({ opacity: getComputedStyle(el).opacity, background: getComputedStyle(el).backgroundColor }))).toEqual({ opacity: "1", background: "rgb(11, 21, 32)" });
    await page.getByTestId("video-stage").scrollIntoViewIfNeeded();
    await inset.screenshot({ path: testInfo.outputPath("field-compact.png") });
    await page.screenshot({ path: testInfo.outputPath("field-in-context.png"), fullPage: true });
    await inset.getByRole("button", { name: "Move field view to next corner" }).click();
    await expect(inset).toHaveAttribute("data-corner", "0");
    await inset.getByRole("button", { name: "Expand field view" }).click();
    await expect(inset).toHaveAttribute("data-expanded", "true");
    const bounds = await inset.boundingBox(); const stage = await page.getByTestId("video-stage").boundingBox();
    expect(bounds!.width).toBeLessThan(stage!.width);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(stage!.x + stage!.width);
    await inset.screenshot({ path: testInfo.outputPath("field-expanded.png") });
    await inset.getByRole("button", { name: "Hide field view" }).click();
    await expect(inset.locator("svg[data-testid='field-diagram']")).toHaveCount(0);
    await expect(inset.getByRole("button", { name: "Show field view" })).toBeFocused();
    const seekState = await video.evaluate((el: HTMLVideoElement) => {
      const before = { time: el.currentTime, duration: el.duration, readyState: el.readyState, seekable: Array.from({ length: el.seekable.length }, (_, i) => [el.seekable.start(i), el.seekable.end(i)]) };
      el.currentTime = 1;
      return { before, after: el.currentTime };
    });
    await testInfo.attach("native-seek-state", { body: JSON.stringify(seekState), contentType: "application/json" });
    await expect.poll(() => video.evaluate((el: HTMLVideoElement) => el.currentTime)).toBeCloseTo(1, 2);
    await inset.getByRole("button", { name: "Show field view" }).click();
    await expect(inset.locator("circle")).toHaveCount(0);
    await expect(inset.getByRole("button", { name: "Hide field view" })).toBeFocused();
    await expect(inset).toHaveAttribute("data-corner", "0");
    expect(await video.evaluate((el: HTMLVideoElement) => el.paused)).toBe(true);
    expect(errors).toEqual([]);
  });
}


test("practice referee review stays separate and calibration remains unavailable", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await serveNativeFixture(page);
  const points = [{ frame_number: 0, timestamp_seconds: 0, shot_id: "0", visibility_state: "observed", bbox: [300, 180, 320, 240], field_x: null, field_y: null }];
  await mockBackend(page, {
    "GET /api/v1/*": [],
    "GET /api/v1/clips/c-1": sampleClip({ start_time: 0, end_time: 2 }),
    "GET /api/v1/videos/v-1": sampleVideo({ filename: "SYNTHETIC_REFEREE_UI_TEST.mp4", session_kind: "practice", library_role: "toledo", width: 960, height: 540 }),
    "GET /api/v1/videos/download-url": { downloadUrl: "https://media.e2e.local/field-native.mp4" },
    "GET /api/v1/clips/c-1/overlays": sampleOverlays({
      calibration: { analytics_safe: false, confidence: .2, reason: "Camera calibration missing", reason_codes: ["missing"] },
      layers_available: { tracklets: true, events: false, labels: false, metrics: false },
      tracklets: [sampleTracklet({ id: "player", track_points: points }), sampleTracklet({ id: "referee", team_label: "official", provenance: { actor_kind: "official" }, track_points: points }), sampleTracklet({ id: "away-group", team_label: "away", track_points: points })],
    }),
  });
  await page.goto("/clip-review/?clipId=c-1");
  const inset = page.getByTestId("field-inset");
  await expect(inset.getByText("Field unavailable")).toBeVisible();
  await expect(inset.getByTestId("field-diagram")).toHaveCount(0);
  await inset.screenshot({ path: testInfo.outputPath("field-unavailable.png") });
  await page.getByRole("tab", { name: "Players" }).click();
  const inspector = page.getByTestId("tracking-inspector");
  await expect(inspector.getByText("Our practice group 1", { exact: true }).first()).toBeVisible();
  await expect(inspector.getByText("Our practice group 2", { exact: true }).first()).toBeVisible();
  await inspector.getByRole("button", { name: "Referees", exact: true }).click();
  await inspector.getByRole("button", { name: "Inspect referee candidate T2", exact: true }).click();
  await expect(page.getByTestId("selected-player-inspector")).toContainText("Non-player");
  await inspector.screenshot({ path: testInfo.outputPath("referee-inspector.png") });
  await inspector.getByRole("button", { name: "Review person kind" }).click();
  await expect(page.getByRole("tab", { name: "Tag", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("Person kind", { exact: true })).toBeVisible();
  await expect(page.getByTestId("correction-player-select")).toBeDisabled();
  await page.getByLabel("Person kind", { exact: true }).selectOption("player");
  await expect(page.getByTestId("correction-player-select")).toBeEnabled();
});
