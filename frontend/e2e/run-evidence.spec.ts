import { expect, test } from "@playwright/test";
import { mockBackend } from "./helpers";
import fixture from "../src/components/__fixtures__/run-evidence-producer.json";

const evidence = (row: { tracklet_id: string; metric_value: Record<string, unknown> }, index: number) => ({
  metric_id: `run-${index}`, play_id: "p-1", clip_id: "c-1", video_id: "v-1", start_seconds: 0, end_seconds: 3,
  tracklet_id: row.tracklet_id, result_version: "run", metric_packet: row.metric_value,
});
const views = { play_id: "p-1", views: [{ clip_id: "c-1", video_id: "v-1", start_seconds: 0, end_seconds: 3, camera_label: "Carrier source" }], metrics: [] };

test("producer-backed RB path, contact, original gap and density seek the actual source", async ({ page }) => {
  const errors: string[] = []; page.on("pageerror", (error) => errors.push(error.message));
  await mockBackend(page, {
    "GET /api/v1/*": [], "GET /api/v1/metrics/definitions": { definitions: [] },
    "POST /api/v1/analytics/query": {
      dataset_version: "synthetic-producer-browser-contract", cohort: { plays: 1, player_reps: 1, eligible_measurements: 1, excluded_measurements: 0 }, groups: [], exclusions: [],
      products: fixture.rows.filter((r) => r.metric_name === "observed_run_path").map((r, i) => ({ metric_name: r.metric_name, unit: r.unit, definition_version: r.metric_value.definition_version, evidence: [evidence(r, i)] })),
    }, "GET /api/v1/plays/p-1/evidence": views,
  });
  await page.goto("/team-stats/?position=RB&session_kind=practice");
  const map = page.getByRole("img", { name: "Observed run density, selected path and explicit contact map" });
  await expect(map).toBeVisible(); await expect(map.locator("polyline")).toHaveCount(1);
  expect(await map.innerHTML()).not.toMatch(/NaN|Infinity/);
  await expect(page.getByText("left A: 1 observed play")).toBeVisible();
  await page.getByRole("button", { name: "Inspect first contact at 1.20s" }).click();
  const link = page.getByRole("link", { name: /^Carrier source/ }); await expect(link).toBeVisible();
  await expect.poll(async () => new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("1.2");
  await page.getByRole("button", { name: "Inspect original snap geometry" }).click();
  await expect.poll(async () => new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("0");
  await page.getByRole("button", { name: /Inspect actual LOS entry/ }).click();
  await expect.poll(async () => new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("1");
  await page.getByText("Measured path and density film evidence", { exact: true }).click();
  await page.getByRole("button", { name: "Inspect path at 0.300s" }).click();
  await expect.poll(async () => new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("0.3");
  await page.getByRole("button", { name: /Inspect density cell/ }).first().click();
  expect(new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("position")).toBe("RB");
  expect(errors).toEqual([]);
});

test("loss-prefix RB evidence has source drilldown without inferred contact, gap or completed outcome", async ({ page }) => {
  await mockBackend(page, {
    "GET /api/v1/*": [], "GET /api/v1/metrics/definitions": { definitions: [] },
    "POST /api/v1/analytics/query": {
      dataset_version: "synthetic-producer-prefix-contract", cohort: { plays: 1, player_reps: 1, eligible_measurements: 0, excluded_measurements: 0 }, groups: [], exclusions: [],
      products: fixture.prefix_rows.map((r, i) => ({ metric_name: r.metric_name, unit: r.unit, definition_version: r.metric_value.definition_version, evidence: [evidence(r, i)] })),
    }, "GET /api/v1/plays/p-1/evidence": views,
  });
  await page.goto("/team-stats/?position=RB&session_kind=practice");
  await expect(page.getByText(/Partial observed prefix; no complete run outcome/)).toBeVisible();
  await expect(page.getByText("First contact unavailable", { exact: true })).toBeVisible();
  await expect(page.getByText("Actual LOS entry unavailable", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Inspect first contact/ })).toHaveCount(0);
  await page.getByText("Measured path and density film evidence", { exact: true }).click();
  await page.getByRole("button", { name: "Inspect path at 0.800s" }).click();
  const link = page.getByRole("link", { name: /^Carrier source/ }); await expect(link).toBeVisible();
  expect(new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("0.8");
});

test("uncalibrated RB selection keeps unavailable evidence quiet and does not invent labels", async ({ page }) => {
  await mockBackend(page, {
    "GET /api/v1/*": [], "GET /api/v1/metrics/definitions": { definitions: [] },
    "POST /api/v1/analytics/query": { dataset_version: "synthetic-unavailable-contract", cohort: { plays: 1, player_reps: 0, eligible_measurements: 0, excluded_measurements: 1 }, groups: [], products: [], exclusions: [{ reason: "calibration_not_safe", count: 1 }] },
  });
  await page.goto("/team-stats/?position=RB");
  await expect(page.getByText(/Qualified carrier paths are unavailable/)).toBeVisible();
  await expect(page.getByRole("img", { name: "Observed run density, selected path and explicit contact map" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Inspect first contact|Inspect actual LOS entry/ })).toHaveCount(0);
  await expect(page.getByText("No qualified measurements in this cohort", { exact: true })).toBeVisible();
});
