import { expect, test } from "@playwright/test";
import {
  mockBackend,
  samplePracticeSession,
  sampleClip,
  sampleVideo,
} from "./helpers";

/**
 * Seeded backend → server-backed Library shows the practice session,
 * its videos, and the clips that link to /clip-review.
 */
test("library renders seeded sessions, videos, and clips", async ({ page }) => {
  await mockBackend(page, {
    "GET /api/v1/practice-sessions": [samplePracticeSession()],
    "GET /api/v1/videos": [sampleVideo()],
    "GET /api/v1/jobs": [],
    "GET /api/v1/self-scout/tendencies": { tendencies: [] },
    "GET /api/v1/inbox/status": [],
    "GET /api/v1/videos/v-1/clips": [sampleClip()],
  });

  await page.goto("/film-room/?tab=browse");

  // Section heading is present and the session row renders.
  await expect(
    page.getByRole("heading", { name: /Film Library/ }),
  ).toBeVisible();
  const sessionToggle = page.getByRole("button", {
    name: /Expand session/,
  });
  await expect(sessionToggle).toBeVisible();
  await sessionToggle.click();

  // Video row from the seeded payload.
  await expect(page.getByText("PR_20251001_DRONEA.mp4")).toBeVisible();

  // Expand clips for that video, then assert the clip link to Clip Review.
  await page.getByRole("button", { name: /Show clips/ }).click();
  const clipLink = page.getByRole("link", { name: /Play #1/ });
  await expect(clipLink).toBeVisible();
  await expect(clipLink).toHaveAttribute(
    "href",
    /\/clip-review\/\?clipId=c-1/,
  );
});
