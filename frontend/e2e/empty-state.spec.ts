import { expect, test } from "@playwright/test";
import { mockBackend } from "./helpers";

/**
 * With an empty backend (no videos, jobs, or inbox rows), the dashboard
 * must not silently substitute mock clips. The Practice Inbox panel
 * should surface an explicit empty/loading message and no rows from
 * `frontend/src/lib/mock-data.ts` should appear.
 *
 * The library route, viewed against an empty backend, must surface the
 * "No film yet" empty state instead of mock sessions.
 */
test.describe("empty backend never shows mock clips", () => {
  test.beforeEach(async ({ page }) => {
    await mockBackend(page, {
      "GET /api/v1/videos": [],
      "GET /api/v1/jobs": [],
      "GET /api/v1/self-scout/tendencies": { tendencies: [] },
      "GET /api/v1/inbox/status": [],
      "GET /api/v1/practice-sessions": [],
    });
  });

  test("dashboard inbox shows empty state, not mock rows", async ({ page }) => {
    await page.goto("/");
    const inbox = page
      .locator("section")
      .filter({ hasText: /Practice Inbox/ });
    await expect(inbox).toContainText(/No videos in the inbox yet|Connecting/);
    // Mock data filenames must not leak into the live view.
    await expect(inbox).not.toContainText(/MOCK_|sample_practice/i);
  });

  test("library shows empty state for empty backend", async ({ page }) => {
    await page.goto("/film-room/?tab=browse");
    await expect(
      page.getByRole("heading", { name: /No film yet/ }),
    ).toBeVisible();
  });
});
