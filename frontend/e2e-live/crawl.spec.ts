/**
 * Click-through of the deployed platform as the shared team account.
 *
 * Every main page must load without console errors, failed API calls or
 * raw error text (status codes, JSON bodies, stack traces) on screen.
 */
import { expect, test, type Page } from "@playwright/test";
import { LIVE_API_URL } from "../playwright.live.config";

const EMAIL = process.env.E2E_EMAIL ?? "";
const PASSWORD = process.env.E2E_PASSWORD ?? "";

/** Text that means an error reached the screen unformatted. */
const RAW_ERROR = /failed \(\d{3}\)|\{"detail"|Traceback|undefined is not|\bNaN\b|\[object Object\]/;

const ROUTES = [
  "/",
  "/film-room/?tab=upload",
  "/film-room/?tab=browse",
  "/film-room/?tab=review",
  "/scouting/?tab=tendencies",
  "/scouting/?tab=opponent",
  "/scouting/?tab=college",
  "/players/?tab=roster",
  "/players/?tab=development",
  "/players/?tab=leaderboard",
  "/team-stats/",
  "/health-workload/",
  "/analytics/",
  "/reports/",
  "/alerts/",
  "/settings/",
];

interface Problem {
  page: string;
  kind: string;
  text: string;
}

function watch(page: Page, current: () => string): Problem[] {
  const problems: Problem[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      problems.push({ page: current(), kind: "console", text: message.text().slice(0, 300) });
    }
  });
  page.on("pageerror", (error) => {
    problems.push({ page: current(), kind: "pageerror", text: String(error).slice(0, 300) });
  });
  page.on("response", (response) => {
    if (response.url().startsWith(LIVE_API_URL) && response.status() >= 400) {
      const path = response.url().slice(LIVE_API_URL.length).slice(0, 160);
      problems.push({
        page: current(),
        kind: "http",
        text: `${response.status()} ${response.request().method()} ${path}`,
      });
    }
  });
  return problems;
}

async function signIn(page: Page): Promise<string> {
  await page.goto("/login/");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  // Fail at once, and say why, when the site rejects the login (wrong
  // E2E_EMAIL / E2E_PASSWORD secrets) instead of timing out on the URL.
  // (Next.js keeps an empty role="alert" route announcer on every page.)
  const rejected = page.getByRole("alert").filter({ hasText: /\S/ }).first();
  let outcome = "waiting";
  await expect
    .poll(
      async () => {
        if (!new URL(page.url()).pathname.startsWith("/login")) outcome = "signed-in";
        else if (await rejected.isVisible()) outcome = `rejected: ${(await rejected.innerText()).trim()}`;
        return outcome;
      },
      { timeout: 120_000, intervals: [500] },
    )
    .not.toBe("waiting");
  expect(outcome, "Check the E2E_EMAIL and E2E_PASSWORD repository secrets.").toBe("signed-in");
  const token = await page.evaluate(() => {
    const raw = window.localStorage.getItem("football-iq-auth-v1");
    return raw ? (JSON.parse(raw).token as string | undefined) ?? null : null;
  });
  expect(token, "signing in stores a session").toBeTruthy();
  return token as string;
}

/** Wait for loading indicators to settle, so late errors are caught. */
async function settle(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => undefined);
  await page.waitForTimeout(1500);
}

test.describe("deployed platform", () => {
  test.skip(!EMAIL || !PASSWORD, "Set E2E_EMAIL and E2E_PASSWORD to check the deployed site.");

  test("the API runs the expected release", async ({ request }) => {
    // After a deploy the new container rolls out over a few minutes.
    const expected = process.env.E2E_EXPECT_SHA || undefined;
    test.setTimeout(expected ? 1_200_000 : 480_000);
    await expect
      .poll(
        async () => {
          const response = await request.get(`${LIVE_API_URL}/api/v1/system/version`);
          if (!response.ok()) return `HTTP ${response.status()}`;
          const body = (await response.json()) as { git_sha?: string };
          return expected ? body.git_sha : "up";
        },
        { timeout: expected ? 840_000 : 120_000, intervals: [15_000] },
      )
      .toBe(expected ?? "up");
    // A deploy can bounce the container more than once while it settles:
    // only crawl once the API has answered steadily for about a minute.
    let steady = 0;
    await expect
      .poll(
        async () => {
          const response = await request.get(`${LIVE_API_URL}/api/v1/system/version`);
          steady = response.ok() ? steady + 1 : 0;
          return steady;
        },
        { timeout: 300_000, intervals: [10_000] },
      )
      .toBeGreaterThanOrEqual(6);
  });

  test("every page loads cleanly for the team account", async ({ page }) => {
    let current = "/login/";
    const problems = watch(page, () => current);
    const token = await signIn(page);

    const headers = { Authorization: `Bearer ${token}` };
    const playersResponse = await page.request.get(
      `${LIVE_API_URL}/api/v1/players?is_active=true&limit=1`,
      { headers },
    );
    await expect(playersResponse, "the roster lookup succeeds").toBeOK();
    const players = await playersResponse.json();
    const playerId = (Array.isArray(players) ? players : players?.items ?? [])[0]?.id;
    const routes = playerId ? [...ROUTES, `/players/detail/?id=${playerId}`] : ROUTES;

    for (const route of routes) {
      current = route;
      await page.goto(route, { waitUntil: "domcontentloaded" });
      await settle(page);
      const text = await page.evaluate(() => document.body.innerText);
      const raw = text.match(RAW_ERROR);
      if (raw && raw.index !== undefined) {
        problems.push({
          page: route,
          kind: "raw-text",
          text: text.slice(Math.max(0, raw.index - 80), raw.index + 120).replace(/\s+/g, " "),
        });
      }
      await expect(page.locator("h1, h2").first(), `${route} renders a heading`).toBeVisible();
      if (route === "/") {
        // Home is the team dashboard: its sections render (with data or an
        // honest empty state), never the "didn't load" fallback.
        for (const section of ["Performance trend", "Position groups", "Games", "Readiness"]) {
          await expect(page.getByRole("heading", { name: section }), `Home shows ${section}`).toBeVisible();
        }
      }
    }

    expect(problems, JSON.stringify(problems, null, 1)).toEqual([]);
  });
});
