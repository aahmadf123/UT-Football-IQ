/**
 * Playwright configuration for checks against the deployed platform.
 *
 * Unlike `playwright.config.ts` (fake backend, intercepted network), this
 * suite signs in to the real site with the shared team account and asserts
 * that people see working pages: no console errors, no failed API calls and
 * no raw error text. It needs:
 *
 *   E2E_BASE_URL   the deployed frontend (default: the Vercel production URL)
 *   E2E_API_URL    the deployed API edge (default: production)
 *   E2E_EMAIL      the shared team login
 *   E2E_PASSWORD   its password (a CI secret; never commit it)
 *
 * Run with `npm run e2e:live`. It only reads; it never uploads or edits.
 */
import { defineConfig, devices } from "@playwright/test";

export const LIVE_BASE_URL = process.env.E2E_BASE_URL ?? "https://ut-football-iq.vercel.app";
export const LIVE_API_URL =
  process.env.E2E_API_URL ?? "https://footiq-api-edge.firas-azfar.workers.dev";

export default defineConfig({
  testDir: "./e2e-live",
  fullyParallel: false,
  workers: 1,
  // A cold container takes a while to answer the first request.
  timeout: 180_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: LIVE_BASE_URL,
    trace: "retain-on-failure",
    actionTimeout: 20_000,
    navigationTimeout: 60_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
          ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } }
          : {}),
      },
    },
  ],
});
