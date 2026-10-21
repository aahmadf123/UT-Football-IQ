import { expect, test } from "@playwright/test";
import { EMPTY_DASHBOARD, mockBackend } from "./helpers";

/**
 * With an empty backend (no videos, jobs, or inbox rows), Home must not
 * silently substitute sample numbers: it says what would fill it, and no
 * rows from `frontend/src/lib/mock-data.ts` appear.
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
      "GET /api/v1/team/dashboard": EMPTY_DASHBOARD,
    });
  });

  test("home shows empty states, not mock rows", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("No measured film in this range yet")).toBeVisible();
    await expect(page.getByText(/No game film is linked/)).toBeVisible();
    // Mock data filenames must not leak into the live view.
    await expect(page.locator("main")).not.toContainText(/MOCK_|sample_practice/i);
  });

  test("library shows empty state for empty backend", async ({ page }) => {
    await page.goto("/film-room/?tab=browse");
    await expect(
      page.getByRole("heading", { name: /No film yet/ }),
    ).toBeVisible();
  });
});
