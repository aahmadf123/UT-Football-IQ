import { expect, test } from "@playwright/test";
import { mockBackend, sampleInboxItem } from "./helpers";

/**
 * Seeded /api/v1/inbox/status response → Film Room's upload & processing
 * list surfaces the row and status returned by the backend.
 */
test("practice inbox renders seeded video status", async ({ page }) => {
  await mockBackend(page, {
    "GET /api/v1/videos": [],
    "GET /api/v1/jobs": [],
    "GET /api/v1/self-scout/tendencies": { tendencies: [] },
    "GET /api/v1/inbox/status": [
      sampleInboxItem({
        video_id: "vid-inbox-1",
        filename: "PR_20251015_DRONEA.mp4",
        video_status: "processing",
        succeeded_jobs: 3,
        total_jobs: 6,
        clip_count: 0,
      }),
    ],
  });

  await page.goto("/film-room/?tab=upload");

  const row = page.getByText("PR_20251015_DRONEA.mp4").first();
  await expect(row).toBeVisible();
  await expect(page.locator("main")).toContainText(/processing/i);
});
