import { expect, test } from "@playwright/test";
import {
  mockBackend,
  type Route,
  sampleClip,
  sampleOverlays,
  sampleTracklet,
  sampleVideo,
} from "./helpers";

/**
 * Clip Review corrections panel (Hudl-lite labeling): a coach expands the
 * side panel, picks the tracklet by its T{n} tag, enters the fix, and the
 * panel POSTs the backend correction contract to /api/v1/corrections. The
 * mocked backend records the payload so we can assert the contract, and the
 * UI confirms with the learning-loop message and clears the form.
 */
test("coach submits a player-identity correction through the side panel", async ({
  page,
}) => {
  let received: unknown = null;
  await mockBackend(page, {
    "GET /api/v1/videos": [],
    "GET /api/v1/jobs": [],
    "GET /api/v1/self-scout/tendencies": { tendencies: [] },
    "GET /api/v1/inbox/status": [],
    "GET /api/v1/clips/c-1": sampleClip(),
    "GET /api/v1/clips/c-1/overlays": sampleOverlays({
      tracklets: [
        sampleTracklet(),
        sampleTracklet({ id: "t-2", team_label: "away", position_group: null }),
      ],
      layers_available: { tracklets: true, events: false, labels: false, metrics: false },
    }),
    "GET /api/v1/videos/v-1": sampleVideo(),
    "POST /api/v1/corrections": async (route: Route) => {
      received = JSON.parse(route.request().postData() ?? "{}");
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          id: "cc-1",
          clip_id: "c-1",
          correction_type: "player_identity",
          original_value: { tracklet_id: "t-2", team: "away" },
          corrected_value: { tracklet_id: "t-2", team: "home", jersey_number: 22 },
          corrected_by: "u-1",
          notes: "jersey misread",
          training_eligible: true,
          exported_as_label: false,
          created_at: "2026-07-24T10:00:00Z",
        }),
      });
    },
  });

  await page.goto("/clip-review/?clipId=c-1");

  // The panel is collapsed by default so it doesn't crowd the player.
  await page.getByTestId("corrections-toggle").click();

  // Default kind is "Wrong team / jersey number"; pick the track by its
  // T{n} tag, then enter the fix.
  await page.locator("#correction-tracklet").selectOption("t-2");
  await page.locator("#correction-team").selectOption("home");
  await page.locator("#correction-jersey").fill("22");
  await page.locator("#correction-note").fill("jersey misread");
  await page.getByTestId("correction-submit").click();

  await expect(page.getByTestId("correction-success")).toContainText(
    "feeds the nightly learning loop",
  );

  expect(received).toMatchObject({
    clip_id: "c-1",
    correction_type: "player_identity",
    corrected_value: { tracklet_id: "t-2", team: "home", jersey_number: 22 },
    original_value: { tracklet_id: "t-2", team: "away" },
    notes: "jersey misread",
  });

  // Form cleared for the next correction.
  await expect(page.locator("#correction-jersey")).toHaveValue("");
  await expect(page.locator("#correction-tracklet")).toHaveValue("");
});

/**
 * A backend rejection (e.g. an expired session downgraded to viewer → 403)
 * must surface inline in the panel, not crash the review page.
 */
test("correction failure shows the error inline", async ({ page }) => {
  await mockBackend(page, {
    "GET /api/v1/videos": [],
    "GET /api/v1/jobs": [],
    "GET /api/v1/self-scout/tendencies": { tendencies: [] },
    "GET /api/v1/inbox/status": [],
    "GET /api/v1/clips/c-1": sampleClip(),
    "GET /api/v1/clips/c-1/overlays": sampleOverlays(),
    "GET /api/v1/videos/v-1": sampleVideo(),
    "POST /api/v1/corrections": async (route: Route) => {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ detail: "Role 'viewer' is not permitted for this action" }),
      });
    },
  });

  await page.goto("/clip-review/?clipId=c-1");

  await page.getByTestId("corrections-toggle").click();
  await page.locator("#correction-kind").selectOption("formation_tag");
  await page.locator("#correction-formation").fill("trips right");
  await page.getByTestId("correction-submit").click();

  await expect(page.getByTestId("correction-error")).toContainText("403");
  // The review shell is still alive.
  await expect(page.getByTestId("overlay-toggles")).toBeVisible();
});
