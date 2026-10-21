import { expect, test } from "@playwright/test";
import { mockBackend, sampleClip, sampleOverlays, sampleVideo, type Route } from "./helpers";
import originalFront from "../src/components/__fixtures__/original-front-producer.json";
import trenchPair from "../src/components/__fixtures__/trench-interaction-producer.json";

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
  await expect(page).toHaveURL(/[?&]view=saved-1(?:&|$)/);
  await page.getByText("Contributing plays and accessible data table").click();
  await page.getByRole("link", { name: "Watch play", exact: true }).click();
  await expect(page).toHaveURL(/clipId=c-1/);
  expect(new URL(page.url()).searchParams.get("position")).toBe("WR");
  await expect(page.getByRole("link", { name: "Back", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Back", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Team Analytics", exact: true })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("position")).toBe("WR");
});

test("measured gap heatmap renders actual SVG cells and links the selected evidence", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockBackend(page, {
    "GET /api/v1/*": [],
    "GET /api/v1/metrics/definitions": { definitions: [{ name: "clear_lane_width", label: "Clear lane width", definition: "Measured clear section at LOS", unit: "yd", version: "v1", value_kind: "observed", availability: "qualified" }] },
    "POST /api/v1/analytics/query": { dataset_version: "synthetic-browser-fixture", cohort: { plays: 1, player_reps: 1, eligible_measurements: 1, excluded_measurements: 0 }, exclusions: [], groups: [{ key: "OL", metric_name: "clear_lane_width", unit: "yd", n: 1, mean: 1, median: 1, p10: 1, p90: 1, min: 1, max: 1, low_sample: true, definition_version: "v1", evidence: [{ play_id: "p-1", clip_id: "c-1", metric_id: "m-1", value: 1, video_id: "v-1", start_seconds: 10, end_seconds: 20, result_version: "run", metric_packet: { gap_label: "Left A", subject_id: "C-LG", measurement_region: { cross_section: "LOS", los_x: 50, y_min: -3, y_max: 0 }, series: [{ timestamp_seconds: 12, width_yards: 1.25 }, { timestamp_seconds: 12.1, width_yards: 0 }] } }] }] },
    "GET /api/v1/plays/p-1/evidence": { play_id: "p-1", views: [{ clip_id: "c-1", video_id: "v-1", start_seconds: 10, end_seconds: 20, camera_label: "Drone" }], metrics: [] },
  });
  await page.goto("/team-stats/?position=OL&session_kind=practice");
  const chart = page.getByRole("img", { name: /Gap width heatmap/ });
  await expect(chart.locator("svg")).toBeVisible();
  const shapes = await chart.locator("svg path").evaluateAll((paths) => paths.map((path) => ({ d: path.getAttribute("d"), fill: path.getAttribute("fill") })));
  expect(shapes.some((shape) => shape.fill && shape.fill !== "none" && shape.d?.startsWith("M"))).toBe(true);
  expect(shapes.some((shape) => /NaN|Infinity/.test(shape.d ?? ""))).toBe(false);
  await expect(page.getByText("Chart unavailable. Use the data table below.")).toHaveCount(0);
  await page.getByText("Measured sections and film evidence").click();
  await page.getByRole("button", { name: "Inspect section" }).first().click();
  const link = page.getByRole("link", { name: /^Drone/ });
  await expect(link).toBeVisible();
  const target = new URL((await link.getAttribute("href"))!, "https://film.test");
  expect(target.searchParams.get("t")).toBe("2");
  expect(target.searchParams.get("position")).toBe("OL");
  expect(errors).toEqual([]);
});


test("movement observations and opportunities open the exact contributing source instant", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const row = { play_id: "p-1", clip_id: "c-1", metric_id: "separation", value: 3, video_id: "v-1", start_seconds: 10, end_seconds: 20, tracklet_id: "receiver", player_id: "player", result_version: "run" };
  await mockBackend(page, {
    "GET /api/v1/*": [],
    "GET /api/v1/metrics/definitions": { definitions: [] },
    "POST /api/v1/analytics/query": { dataset_version: "synthetic-browser-fixture", cohort: { plays: 2, player_reps: 2, eligible_measurements: 1, excluded_measurements: 0 }, exclusions: [], groups: [{ key: "WR", metric_name: "observed_separation", unit: "yd", n: 1, mean: 3, median: 3, p10: 3, p90: 3, min: 3, max: 3, low_sample: true, definition_version: "v1", evidence: [{ ...row, metric_packet: { comparison_scope: "explicit_comparison_defender", series: [{ timestamp_seconds: 12, yards: 3, actor_id: "receiver", opponent_id: "defender", shot_id: "a", visibility_state: "observed" }, { timestamp_seconds: 12.1, yards: 3.5, actor_id: "receiver", opponent_id: "defender", shot_id: "a", visibility_state: "observed" }] } }] }], opportunities: [{ kind: "routes", definition_version: "v1", eligible_count: 2, player_count: 1, evidence: [{ ...row, metric_id: "participation", metric_packet: { source_start_seconds: 11 } }] }] },
    "GET /api/v1/plays/p-1/evidence": { play_id: "p-1", views: [{ clip_id: "c-1", video_id: "v-1", start_seconds: 10, end_seconds: 20, camera_label: "Sideline" }], metrics: [] },
  });
  await page.goto("/team-stats/?position=WR&session_kind=practice");
  const chart = page.getByRole("img", { name: /observed separation in yd/ });
  await expect(chart.locator("svg")).toBeVisible();
  expect(await chart.locator("svg").innerHTML()).not.toMatch(/NaN|Infinity/);
  await page.getByText("Observed values and comparison actors").click();
  await page.getByRole("button", { name: "Inspect pair at 12.10s" }).click();
  const link = page.getByRole("link", { name: /^Sideline/ });
  await expect(link).toBeVisible();
  expect(Number(new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t"))).toBeCloseTo(2.1);
  await page.getByText("Inspect opportunities", { exact: true }).click();
  await page.getByRole("button", { name: "Inspect routes rep 1" }).click();
  await expect.poll(async () => new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("1");
  expect(errors).toEqual([]);
});


test("measured envelope controls and interaction matrix remain linked to exact film evidence", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const row = { play_id: "p-1", clip_id: "c-1", metric_id: "envelope", video_id: "v-1", start_seconds: 10, end_seconds: 20, result_version: "run" };
  const polygon = (right: number) => [{ field_x: 40, field_y: -3 }, { field_x: right, field_y: -3 }, { field_x: right, field_y: 3 }, { field_x: 40, field_y: 3 }];
  const event = { event_id: "geometry-1", event_type: "protection_envelope", source: "human", timestamp_seconds: 11, shot_id: "a" };
  const second = { ...event, event_id: "geometry-2", timestamp_seconds: 11.1 };
  await mockBackend(page, {
    "GET /api/v1/*": [], "GET /api/v1/metrics/definitions": { definitions: [] },
    "POST /api/v1/analytics/query": { dataset_version: "synthetic-browser-fixture", cohort: { plays: 1, player_reps: 1, eligible_measurements: 2, excluded_measurements: 0 }, exclusions: [], products: [{ metric_name: "protection_envelope_area", unit: "yd²", definition_version: "v1", evidence: [{ ...row, metric_packet: { envelope_id: "measured", partial_observation: true, definition_basis: "explicit_measured_protection_polygon", event_evidence: [event, second], series: [{ ...event, polygon: polygon(45), width_yards: 6, depth_yards: 5, area_square_yards: 30, visibility_state: "observed" }, { ...second, polygon: polygon(46), width_yards: 6, depth_yards: 6, area_square_yards: 36, visibility_state: "observed" }] } }] }], groups: [{ key: "interaction", metric_name: "engagement_duration", unit: "s", n: 1, mean: 1, median: 1, p10: 1, p90: 1, min: 1, max: 1, low_sample: true, definition_version: "v1", evidence: [{ ...row, metric_id: "interaction", tracklet_id: "blocker", value: 1, metric_packet: { track_id: "blocker", defender_id: "defender", interaction_id: "observed-pair", source_start_seconds: 11, source_end_seconds: 12, event_evidence: [{ event_id: "start", event_type: "engagement_start", source: "human", timestamp_seconds: 11, shot_id: "a" }, { event_id: "end", event_type: "engagement_end", source: "human", timestamp_seconds: 12, shot_id: "a" }] } }] }] },
    "GET /api/v1/plays/p-1/evidence": { play_id: "p-1", views: [{ clip_id: "c-1", video_id: "v-1", start_seconds: 10, end_seconds: 20, camera_label: "Drone" }], metrics: [] },
  });
  await page.goto("/team-stats/?session_kind=practice");
  await expect(page.getByRole("img", { name: /Measured protection envelope area/ }).locator("svg")).toBeVisible();
  const slider = page.getByRole("slider", { name: "Measured envelope sample" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("img", { name: "Measured polygon at 11.10 source seconds" }).locator("polygon")).toBeVisible();
  await page.getByRole("button", { name: "Inspect envelope at 11.10s" }).click();
  const link = page.getByRole("link", { name: /^Drone/ });
  await expect(link).toBeVisible();
  expect(Number(new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t"))).toBeCloseTo(1.1);
  await page.getByRole("button", { name: "Inspect interaction at 11.00s" }).click();
  await expect.poll(async () => new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("1");
  expect(errors).toEqual([]);
});

test("original five-lineman charts and exact source-frame trench inspection work together", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockBackend(page, {
    "GET /api/v1/*": [], "GET /api/v1/metrics/definitions": { definitions: [] },
    "POST /api/v1/analytics/query": {
      dataset_version: "synthetic-producer-browser-contract", cohort: { plays: 1, player_reps: 5, eligible_measurements: 5, excluded_measurements: 0 }, exclusions: [], groups: [],
      products: [{ metric_name: "lineman_first_second_displacement", unit: "yd", definition_version: "contract", evidence: originalFront.rows.map((row, index) => ({
        metric_id: `front-${index}`, play_id: "p-1", clip_id: "c-1", video_id: "v-1", start_seconds: 0, end_seconds: 2,
        tracklet_id: row.tracklet_id, player_id: `player-${index}`, result_version: "run", metric_packet: row.metric_value,
      })) }],
    },
    "GET /api/v1/plays/p-1/evidence": { play_id: "p-1", views: [{ clip_id: "c-1", video_id: "v-1", start_seconds: 0, end_seconds: 2, camera_label: "Drone" }], metrics: [] },
  });
  await page.goto("/team-stats/?position=OL&session_kind=practice");
  const charts = page.getByRole("img", { name: /observed forward and lateral displacement/ });
  await expect(charts).toHaveCount(5);
  for (const chart of await charts.all()) {
    await expect(chart.locator("svg")).toBeVisible();
    expect(await chart.locator("svg").innerHTML()).not.toMatch(/NaN|Infinity/);
  }
  await page.getByRole("slider", { name: "Trench source sample" }).focus();
  await page.keyboard.press("End");
  await expect(page.getByText("Source frame 10 · 1.000s")).toBeVisible();
  await expect(page.getByRole("img", { name: "Original line at the selected exact source frame" }).locator("circle")).toHaveCount(5);
  await page.getByRole("button", { name: "Inspect film at 1.000s" }).click();
  const link = page.getByRole("link", { name: /^Drone/ });
  await expect(link).toBeVisible();
  expect(new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("1");
  expect(errors).toEqual([]);
});

test("paired trench replay uses native frames and seeks film without filling an absent defender", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockBackend(page, {
    "GET /api/v1/*": [], "GET /api/v1/metrics/definitions": { definitions: [] },
    "POST /api/v1/analytics/query": { dataset_version: "synthetic-producer-browser-contract", cohort: { plays: 1, player_reps: 1, eligible_measurements: 1, excluded_measurements: 0 }, exclusions: [], groups: [{
      key: "OL", metric_name: "engagement_duration", unit: "s", definition_version: "contract", n: 1, mean: .3, median: .3, p10: .3, p90: .3, min: .3, max: .3, low_sample: true,
      evidence: [{ metric_id: "interaction", play_id: "p-1", clip_id: "c-1", video_id: "v-1", start_seconds: 0, end_seconds: 1, value: .3, tracklet_id: "ol", result_version: "run", metric_packet: trenchPair.metric.metric_value }],
    }] },
    "GET /api/v1/plays/p-1/evidence": { play_id: "p-1", views: [{ clip_id: "c-1", video_id: "v-1", start_seconds: 0, end_seconds: 1, camera_label: "Trench source" }], metrics: [] },
  });
  await page.goto("/team-stats/?position=OL&session_kind=practice");
  await expect(page.getByRole("heading", { name: "Trench frame replay" })).toBeVisible();
  await page.getByRole("button", { name: "Next observed frame" }).click();
  await expect(page.getByText("Native frame 1 · 0.100s")).toBeVisible();
  await expect(page.getByRole("img", { name: "Paired trench positions at native frame 1" }).locator("circle")).toHaveCount(1);
  await expect(page.getByRole("region", { name: "Observed trench interaction replay" }).getByText("Unobserved")).toBeVisible();
  await page.getByRole("button", { name: "Inspect paired film at 0.100s" }).click();
  const link = page.getByRole("link", { name: /^Trench source/ });
  await expect(link).toBeVisible();
  expect(new URL((await link.getAttribute("href"))!, "https://film.test").searchParams.get("t")).toBe("0.1");
  expect(errors).toEqual([]);
});
