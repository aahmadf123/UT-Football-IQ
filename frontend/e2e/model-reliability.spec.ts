import { expect, test } from "@playwright/test";
import producer from "../src/components/__fixtures__/model-reliability-producer.json";
import { mockBackendNoAuth, seedAuthSession, type Route } from "./helpers";

test("analyst inspects actual stored diagnostics, reloads and downloads the selected record", async ({ page }) => {
  const errors: string[] = [];
  let reads = 0;
  page.on("pageerror", (error) => errors.push(error.message));
  await mockBackendNoAuth(page, {
    "GET /api/v1/*": [],
    "GET /api/v1/metrics/definitions": { definitions: [] },
    "GET /api/v1/mlops/models": [producer.model],
    "GET /api/v1/learning/evaluations": (route: Route) => {
      expect(route.request().headers().authorization).toMatch(/^Bearer /);
      reads += 1;
      return [producer.evaluation];
    },
    "POST /api/v1/analytics/query": { dataset_version: "synthetic-contract", cohort: { plays: 0, player_reps: 0, eligible_measurements: 0, excluded_measurements: 0 }, groups: [], exclusions: [] },
  });
  await seedAuthSession(page, "analyst");
  await page.goto("/team-stats/?tab=analyst&layout=analyst");
  const panel = page.getByRole("region", { name: "Model reliability evidence" });
  await expect(panel.getByText("Holdout gates did not pass; candidate remains experimental.")).toBeVisible();
  const chart = panel.getByRole("img", { name: "Stored model and baseline calibration bin means" });
  await expect(chart).toBeVisible();
  await expect(chart.locator("circle")).toHaveCount(producer.evaluation.metrics.calibration_bins.length);
  expect(await chart.innerHTML()).not.toMatch(/NaN|Infinity/);
  await expect(panel.getByText(/Recorded probability decision threshold: 0.5/)).toBeVisible();
  const before = reads;
  await panel.getByRole("button", { name: "Reload model reports" }).click();
  await expect.poll(() => reads).toBeGreaterThan(before);
  const downloaded = page.waitForEvent("download");
  await panel.getByRole("button", { name: "Download evaluation record" }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe(`model-evaluation-${producer.evaluation.id}.json`);
  const stream = await download.createReadStream();
  let body = "";
  for await (const chunk of stream) body += chunk.toString();
  expect(JSON.parse(body).stored_evaluation_record).toEqual(producer.evaluation);
  await panel.getByLabel("Report qualification").selectOption("passed");
  await expect(panel.getByText(/No actual stored evaluation matches this model and filter/)).toBeVisible();
  await expect(panel.getByRole("img")).toHaveCount(0);
  expect(errors).toEqual([]);
});
