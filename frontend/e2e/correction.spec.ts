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
    "Available for the learning loop",
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

  await expect(page.getByTestId("correction-error")).toContainText(
    "Role 'viewer' is not permitted for this action",
  );
  // The review shell is still alive.
  await expect(page.getByTestId("overlay-toggles")).toBeVisible();
});


test("staff record a real-track participation observation through the review form", async ({ page }) => {
  let received: Record<string, unknown> | null = null;
  const clip = sampleClip({ start_time: 10, end_time: 20 });
  await mockBackend(page, {
    "GET /api/v1/*": [],
    "GET /api/v1/clips/c-1": clip,
    "GET /api/v1/clips/c-1/overlays": sampleOverlays({ tracklets: [sampleTracklet({ track_points: [{ frame_number: 300, timestamp_seconds: 10, shot_id: "source-shot", visibility_state: "observed", bbox: [10, 10, 20, 30], field_x: null, field_y: null, detection_confidence: .9 }] })] }),
    "GET /api/v1/videos/v-1": sampleVideo(),
    "GET /api/v1/videos/v-1/clips": [clip],
    "POST /api/v1/corrections": (route: Route) => { received = route.request().postDataJSON(); return { id: "observation", ...received, training_eligible: true, created_at: "2026-10-21T00:00:00Z" }; },
  });
  await page.goto("/clip-review/?clipId=c-1");
  await page.getByTestId("corrections-toggle").click();
  await page.getByLabel("What needs fixing?").selectOption("position_observation");
  await page.getByLabel("Observation", { exact: true }).selectOption("position_participation");
  await page.getByLabel("Observed player track").selectOption("t-1");
  await page.getByLabel("Side of the ball", { exact: true }).selectOption("offense");
  await page.getByLabel("Observed role", { exact: true }).selectOption("WR");
  await page.getByLabel("Opportunity", { exact: true }).selectOption("routes");
  await page.getByLabel("Observation time (play seconds)").fill("1");
  await page.getByLabel("Participation end (play seconds)").fill("3");
  await page.getByLabel("Attacking direction", { exact: true }).selectOption("1");
  await page.getByTestId("correction-submit").click();
  await expect.poll(() => received).toMatchObject({ clip_id: "c-1", correction_type: "event_tag", corrected_value: { event_type: "position_participation", timestamp_seconds: 1, attributes: { actor_id: "t-1", observed_role: "WR", team_label: "offense", opportunity_kind: "routes", end_seconds: 3, shot_id: "source-shot", attacking_direction: 1 } } });
});

test("prediction context saves its exact native frame and recording clock from the clip form", async ({ page }) => {
  let received: Record<string, unknown> | null = null;
  const clip = sampleClip({ start_time: 10, end_time: 20 });
  const point = { frame_number: 375, timestamp_seconds: 12.5, shot_id: "native", visibility_state: "observed", bbox: [10, 10, 20, 30] as [number, number, number, number], field_x: null, field_y: null, detection_confidence: .9 };
  const tracks = ["t-1", "t-2", "t-3"].map((id, index) => sampleTracklet({ id, side_of_ball: index === 2 ? "defense" : "offense", team_label: index === 2 ? "defense" : "offense", track_points: [{ ...point }] }));
  await mockBackend(page, {
    "GET /api/v1/*": [], "GET /api/v1/clips/c-1": clip,
    "GET /api/v1/clips/c-1/overlays": sampleOverlays({ tracklets: tracks }),
    "GET /api/v1/videos/v-1": sampleVideo(), "GET /api/v1/videos/v-1/clips": [clip],
    "POST /api/v1/corrections": (route: Route) => { received = route.request().postDataJSON(); return { id: "context", ...received, training_eligible: true, created_at: "2026-10-21T00:00:00Z" }; },
  });
  await page.goto("/clip-review/?clipId=c-1");
  await page.getByTestId("corrections-toggle").click();
  await page.getByLabel("What needs fixing?").selectOption("position_observation");
  await page.getByLabel("Observation", { exact: true }).selectOption("pass_release");
  await page.getByLabel("Observed player track").selectOption("t-1");
  await page.getByLabel("Add prediction-time context").check();
  await page.getByLabel("Prediction source frame").selectOption("12.5");
  await page.getByLabel("Context available at (play seconds)").fill("2");
  await page.getByLabel("Observed play design").selectOption("pass");
  await page.getByLabel("Prediction quarterback track").selectOption("t-1");
  await page.getByLabel("Intended receiver track").selectOption("t-2");
  await page.getByLabel("I observed an aimed forward pass to an offensive player").check();
  await page.getByLabel("Prediction defender T3", { exact: true }).check();
  await page.getByLabel("I reviewed the complete defender population at this clock").check();
  await page.getByTestId("correction-submit").click();
  await expect.poll(() => received).toMatchObject({ clip_id: "c-1", correction_type: "event_tag", corrected_value: {
    event_type: "pass_release", timestamp_seconds: 12.5, frame_number: 375, timebase: "recording",
    attributes: { actor_id: "t-1", qb_id: "t-1", intended_receiver_id: "t-2", shot_id: "native", feature_context_available_at_seconds: 12, defender_track_ids: ["t-3"], defender_population_complete: true },
  } });
});
