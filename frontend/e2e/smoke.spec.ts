import { expect, test } from "@playwright/test";
import { EMPTY_DASHBOARD, mockBackend } from "./helpers";

/**
 * Smoke test: the app boots, renders the football shell and the Home team
 * dashboard without crashing. All backend calls are mocked as empty so the
 * shell renders against a known-empty state.
 */
test("dashboard loads with empty backend responses", async ({ page }) => {
  await mockBackend(page, {
    "GET /api/v1/videos": [],
    "GET /api/v1/jobs": [],
    "GET /api/v1/self-scout/tendencies": { tendencies: [] },
    "GET /api/v1/inbox/status": [],
    "GET /api/v1/team/dashboard": EMPTY_DASHBOARD,
  });

  await page.goto("/");

  // Football-IQ shell renders the consolidated Film Room nav link (ADR 0003).
  await expect(page.getByRole("link", { name: "Film Room", exact: true })).toBeVisible();
  // No JS crash means Home rendered its sections and honest empty states.
  await expect(page.getByRole("heading", { name: "Performance trend" })).toBeVisible();
  await expect(page.getByText("No measured film in this range yet")).toBeVisible();
});
