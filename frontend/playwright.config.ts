/**
 * Playwright configuration for the Football-IQ frontend E2E suite.
 *
 * The default suite is deterministic: it boots `next dev` against a fake
 * backend hostname and intercepts all network traffic with `page.route()`
 * inside each spec. This means CI does not need a real backend, object
 * storage, or auth credentials to run the suite.
 *
 * Live integration verification against a real backend is an explicitly
 * separate, manual exercise (see `frontend/docs/E2E.md`).
 */
import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.E2E_PORT ?? 3100);
const BASE_URL = `http://localhost:${PORT}`;

// Fake host: any URL beginning with this is intercepted by tests with
// `page.route(...)`. It is intentionally non-resolvable so accidental
// un-mocked requests fail loudly instead of reaching a real backend.
export const FAKE_API_URL = "http://api.e2e.local";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Sandboxes with a system-provided Chromium (and no network path to
        // Playwright's CDN) set PLAYWRIGHT_EXECUTABLE_PATH instead of running
        // `npm run e2e:install`. CI leaves it unset and uses the managed
        // browser download.
        ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
          ? { launchOptions: { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } }
          : {}),
      },
    },
  ],
  webServer: {
    // Use the locally-installed next binary directly; `npx` can prompt to
    // install or fail in network-restricted containers.
    command: `node node_modules/next/dist/bin/next dev -p ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_API_URL: FAKE_API_URL,
      // Keep mocks OFF — the suite must exercise the live code paths and
      // assert that no mock clips leak when the backend has no data.
      NEXT_PUBLIC_USE_MOCKS: "",
    },
  },
});
