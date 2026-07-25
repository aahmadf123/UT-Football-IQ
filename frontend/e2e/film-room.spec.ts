import { expect, test } from "@playwright/test";
import { mockBackend, type Route, sampleInboxItem } from "./helpers";

/**
 * Film Room consolidation (ADR 0003 / #184) + explicit upload-to-processing
 * (#187). Uploaded film surfaces in Film Room → Upload / Process Film with a
 * clear "Process Film" CTA; clicking it enqueues a full orchestrated
 * pipeline job through the backend job API (no Worker/R2 bypass).
 */
test("film room exposes consolidated tabs and an explicit Process Film CTA", async ({
  page,
}) => {
  let processPosted = false;

  await mockBackend(page, {
    "GET /api/v1/videos": [],
    "GET /api/v1/jobs": [],
    "GET /api/v1/players": [],
    "GET /api/v1/self-scout/tendencies": { pre_snap_tells: [], clip_count: 0 },
    "GET /api/v1/inbox/status": [
      sampleInboxItem({
        video_id: "vid-up-1",
        filename: "practice.mp4",
        video_status: "uploaded",
        total_jobs: 0,
        succeeded_jobs: 0,
        clip_count: 0,
      }),
    ],
    "POST /api/v1/jobs": (route: Route) => {
      processPosted = true;
      const body = JSON.parse(route.request().postData() ?? "{}") as {
        video_id: string;
        job_type: string;
      };
      expect(body.video_id).toBe("vid-up-1");
      expect(body.job_type).toBe("pipeline");
      return { id: "job-up-1", status: "queued" };
    },
  });

  await page.goto("/film-room/?tab=upload");

  // The film workflows are consolidated into one destination. The old
  // "Clips & Highlights" mock-clip tab is gone (#96) — real clips live in
  // Browse Film and Review & Tag Plays.
  await expect(page.getByTestId("film-room-tab-browse")).toBeVisible();
  await expect(page.getByTestId("film-room-tab-review")).toBeVisible();
  await expect(page.getByTestId("film-room-tab-upload")).toBeVisible();
  await expect(page.getByTestId("film-room-tab-clips")).toHaveCount(0);

  // Uploaded film is not auto-processed — a clear CTA is offered.
  const cta = page.getByTestId("process-film-vid-up-1");
  await expect(cta).toBeVisible();
  await cta.click();

  // The CTA enqueues through the backend job API and the row reflects Queued.
  await expect.poll(() => processPosted).toBe(true);
  await expect(page.getByTestId("processing-status-vid-up-1")).toContainText(/Queued/i);
});
