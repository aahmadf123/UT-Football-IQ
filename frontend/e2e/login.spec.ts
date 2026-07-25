import { expect, test } from "@playwright/test";
import type { Route } from "@playwright/test";
import { makeE2eJwt, mockBackendNoAuth } from "./helpers";

/**
 * E2E coverage for the login and create-account flows.
 *
 * The suite does NOT pre-seed an auth token so the app redirects to
 * `/login/` as it would for a first-time visitor. Backend calls are
 * intercepted with `mockBackendNoAuth` — no real credentials or backend
 * are needed.
 */

/** Minimal backend stubs needed after a successful login. */
const postLoginRoutes = {
  "POST /api/v1/auth/refresh": {
    access_token: makeE2eJwt("coach"),
    refresh_token: "e2e-refresh-r2",
  },
  "GET /api/v1/videos": [],
  "GET /api/v1/jobs": [],
  "GET /api/v1/self-scout/tendencies": { tendencies: [] },
  "GET /api/v1/inbox/status": [],
};

test("sign-in form: valid credentials redirects to dashboard", async ({ page }) => {
  await mockBackendNoAuth(page, {
    "POST /api/v1/auth/login": {
      access_token: makeE2eJwt("coach"),
      refresh_token: "e2e-refresh-r1",
    },
    ...postLoginRoutes,
  });

  await page.goto("/");
  // Shell detects no token and redirects to /login/
  await page.waitForURL(/\/login\//);
  await expect(page.getByRole("heading", { name: /Football-IQ/ })).toBeVisible();

  await page.getByLabel("Email").fill("coach@utoledo.edu");
  await page.getByLabel("Password").fill("footballiq-password");
  await page.getByRole("button", { name: /Sign in/ }).click();

  // After successful sign-in the app returns to the dashboard.
  await page.waitForURL("/");
  await expect(page.getByRole("link", { name: /Film Room/ })).toBeVisible();
});

test("sign-in form: invalid credentials show an error", async ({ page }) => {
  await mockBackendNoAuth(page, {
    // Simulate a 401 from the backend
    "POST /api/v1/auth/login": async (route: Route) => {
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
        body: JSON.stringify({ detail: "Invalid credentials" }),
      });
    },
  });

  await page.goto("/login");
  await page.getByLabel("Email").fill("nobody@utoledo.edu");
  await page.getByLabel("Password").fill("wrongpassword");
  await page.getByRole("button", { name: /Sign in/ }).click();

  await expect(page.getByRole("form", { name: "Sign in" }).getByRole("alert")).toContainText(
    /Invalid credentials/,
  );
  // Must stay on the login page.
  await expect(page).toHaveURL(/\/login\//);
});

test("create-account tab: registration + auto-login redirects to dashboard", async ({ page }) => {
  await mockBackendNoAuth(page, {
    "POST /api/v1/auth/register": {
      id: "u-new",
      email: "newcoach@utoledo.edu",
      full_name: "Coach New",
      role: "viewer",
      is_active: true,
    },
    "POST /api/v1/auth/login": {
      access_token: makeE2eJwt("viewer"),
      refresh_token: "e2e-refresh-r1",
    },
    ...postLoginRoutes,
    "POST /api/v1/auth/refresh": {
      access_token: makeE2eJwt("viewer"),
      refresh_token: "e2e-refresh-r2",
    },
  });

  await page.goto("/login");

  // Switch to the Create account tab.
  await page.getByRole("tab", { name: /Create account/ }).click();

  await page.getByLabel("Full name").fill("Coach New");
  await page.getByLabel("Email").fill("newcoach@utoledo.edu");
  await page.getByLabel("Password").fill("newpassword1");
  await page.getByRole("button", { name: /Create account/ }).click();

  // After register + auto-login the app returns to the dashboard.
  await page.waitForURL("/");
  await expect(page.getByRole("link", { name: /Film Room/ })).toBeVisible();
});
