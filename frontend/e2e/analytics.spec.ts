import { expect, test } from "@playwright/test";
import { mockBackend, sampleClip, sampleOverlays, sampleVideo, type Route } from "./helpers";

test("cohort chart, saved layout and film drilldown retain context through Back", async ({ page }) => {
  const queries: Record<string, unknown>[] = [];
  let saved: Record<string, unknown> | undefined;
  const definition = { name: "route_depth", label: "Route depth", definition: "Forward distance from LOS", unit: "yd", version: "1", value_kind: "observed", availability: "qualified", required_evidence: ["calibration", "los"] };
  await mockBackend(page, {
    "GET /api/v1/videos": [], "GET /api/v1/jobs": [], "GET /api/v1/players": [], "GET /api/v1/players/metrics/summary": [], "GET /api/v1/inbox/status": [],
    "GET /api/v1/metrics/definitions": { definitions: [definition] },
    "GET /api/v1/saved-views": [],
    "POST /api/v1/saved-views": (route: Route) => { saved = route.request().postDataJSON(); return { ...saved, id: "saved-1", can_edit: true }; },
    "POST /api/v1/analytics/query": (route: Route) => {
      queries.push(route.request().postDataJSON());
      return { dataset_version: "test-fixture-labelled", cohort: { plays: 1, player_reps: 1, eligible_measurements: 1, excluded_measurements: 0, unknown_count: 0 }, groups: [{ key: "WR", metric_name: "route_depth", definition_version: "1", n: 1, mean: 8, median: 8, p10: 8, p90: 8, min: 8, max: 8, unit: "yd", low_sample: true, evidence: [{ play_id: "p-1", clip_id: "c-1", metric_id: "m-1", value: 8, video_id: "v-1", start_seconds: 1, end_seconds: 9 }] }], exclusions: [] };
    },
    "GET /api/v1/clips/c-1": sampleClip(), "GET /api/v1/clips/c-1/overlays": sampleOverlays(),
    "GET /api/v1/videos/v-1": sampleVideo(), "GET /api/v1/videos/v-1/clips": [sampleClip()],
    "GET /api/v1/videos/download-url": { downloadUrl: "https://media.e2e.local/test.mp4" },
  });
  await page.goto("/team-stats/?position=WR&session_kind=practice&layout=analyst");
  await expect(page.getByRole("heading", { name: "Route depth", exact: true })).toBeVisible();
  await expect(page.getByTestId("cohort-denominator")).toContainText("1 canonical plays");
  await expect.poll(() => queries.length).toBeGreaterThan(0);
  expect(queries.at(-1)).toMatchObject({ position: "WR", session_kind: "practice" });
  await page.getByText("Saved views", { exact: true }).click();
  await page.getByLabel("View name").fill("Practice receivers");
  await page.getByRole("button", { name: "Save current view" }).click();
  await expect.poll(() => saved?.name).toBe("Practice receivers");
  expect(saved?.filters).toMatchObject({ position: "WR", session_kind: "practice", layout: "analyst" });
  await page.getByText("Contributing plays and accessible data table").click();
  await page.getByRole("link", { name: "Watch play", exact: true }).click();
  await expect(page).toHaveURL(/clipId=c-1/);
  expect(new URL(page.url()).searchParams.get("position")).toBe("WR");
  await expect(page.getByRole("link", { name: "Back", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Back", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Team Analytics", exact: true })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("position")).toBe("WR");
});
