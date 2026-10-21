/** Navigation contract only: numerical protocol reports never validate football accuracy. */
import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import producer from "../src/components/__fixtures__/model-reliability-cases-producer.json";
import modelFixture from "../src/components/__fixtures__/model-reliability-producer.json";
import { makeE2eJwt, mockBackendNoAuth, sampleClip, sampleOverlays, sampleVideo, seedAuthSession, type Route } from "./helpers";

test("stored reliability source category opens film and Back restores report/cohort/filter then downloads selected cases", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const model = { ...modelFixture.model, id:producer.evaluation.model_id, metrics:{evaluation_id:producer.evaluation.id,artifact_sha256:producer.evaluation.artifact_sha256} };
  const sourceGroup = producer.page.groups.source_group[0].value;
  const linked = structuredClone(producer.page) as unknown as Record<string,unknown>;
  linked.filter = { dimension:"source_group",category:sourceGroup };
  const cases = linked.cases as Record<string,unknown>[];
  // The actual backend current-export/clock/revision match is covered by the
  // real producer HTTP test; this response tests the browser navigation contract.
  cases[0].film = { clip_id:"c-1",focus_seconds:1.25,source_export_job_id:"browser-protocol-export",source_export_sha256:"a".repeat(64) };
  cases[0].film_unavailable_reason = null;
  await mockBackendNoAuth(page, {
    "GET /api/v1/*":[],
    "POST /api/v1/auth/refresh":{access_token:makeE2eJwt("analyst"),refresh_token:"e2e-refresh-token"},
    "GET /api/v1/metrics/definitions":{definitions:[]},
    "POST /api/v1/analytics/query":{dataset_version:"protocol-case-browser-only",cohort:{plays:0,player_reps:0,eligible_measurements:0,excluded_measurements:0},groups:[],exclusions:[]},
    "GET /api/v1/mlops/models":[model],
    "GET /api/v1/learning/evaluations":[producer.evaluation],
    [`GET /api/v1/learning/evaluations/${producer.evaluation.id}/cases`]:(route:Route) => new URL(route.request().url()).searchParams.get("dimension") === "source_group" ? linked : producer.page,
    "GET /api/v1/clips/c-1":sampleClip(),"GET /api/v1/clips/c-1/overlays":sampleOverlays(),
    "GET /api/v1/videos/v-1":sampleVideo(),"GET /api/v1/videos/v-1/clips":[sampleClip()],
    "GET /api/v1/videos/download-url":{downloadUrl:"https://media.e2e.local/protocol-only.mp4"},
  });
  await seedAuthSession(page,"analyst");
  await page.goto("/team-stats/?tab=analyst&layout=analyst&position=QB&session_kind=practice");
  const diagnostic = page.getByRole("region",{name:"Held-out case diagnostics"});
  await expect(diagnostic.getByText(/20 of 20 held-out plays/)).toBeVisible();
  await diagnostic.getByLabel("Held-out case dimension").selectOption("source_group");
  await expect(diagnostic.getByLabel("Held-out case category")).toHaveValue(sourceGroup);
  const film = diagnostic.getByRole("link",{name:"Watch exact source evidence"});
  await expect(film).toBeVisible();
  const destination = new URL((await film.getAttribute("href"))!,"https://fixture.invalid");
  expect(destination.searchParams.get("from")).toContain("/team-stats/?");
  expect(destination.searchParams.get("from")).toContain("reliability_case_dimension=source_group");
  await film.click();
  await expect(page).toHaveURL(/clipId=c-1/);
  expect(new URL(page.url()).searchParams.get("t")).toBe("1.25");
  await page.getByRole("link",{name:"Back",exact:true}).click();
  await expect(page.getByRole("heading",{name:"Team Analytics",exact:true})).toBeVisible();
  await expect(diagnostic.getByLabel("Held-out case dimension")).toHaveValue("source_group");
  await expect(diagnostic.getByLabel("Held-out case category")).toHaveValue(sourceGroup);
  expect(new URL(page.url()).searchParams.get("position")).toBe("QB");
  expect(new URL(page.url()).searchParams.get("tab")).toBe("analyst");
  await expect(page.getByText("Human-approved production binding recorded.")).toHaveCount(0);
  const download = page.waitForEvent("download");
  await diagnostic.getByRole("button",{name:"Download selected cases"}).click();
  const saved = await download;
  expect(saved.suggestedFilename()).toBe(`heldout-cases-${producer.evaluation.id}.json`);
  const body = JSON.parse(await readFile((await saved.path())!,"utf8"));
  expect(body.case_sha256).toBe(producer.page.case_sha256);
  expect(body.filter).toEqual({dimension:"source_group",category:sourceGroup});
  expect(body.cases).toHaveLength(20);
  expect(errors).toEqual([]);
});

test("nondefault model and evaluation survive film Back with All cases and page zero untouched", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const defaultModelId="11111111-1111-4111-8111-111111111111";
  const defaultReportId="22222222-2222-4222-8222-222222222222";
  const alternateReportId="33333333-3333-4333-8333-333333333333";
  const model={...modelFixture.model,id:producer.evaluation.model_id,metrics:{evaluation_id:alternateReportId,artifact_sha256:producer.evaluation.artifact_sha256}};
  const defaultModel={...model,id:defaultModelId,model_name:"default-protocol-model",metrics:{evaluation_id:defaultReportId,artifact_sha256:producer.evaluation.artifact_sha256}};
  const historical={...producer.evaluation,heldout_diagnostics:undefined};
  const linked=structuredClone(producer.page) as unknown as Record<string,unknown>;
  const cases=linked.cases as Record<string,unknown>[];
  // Link availability is a browser contract; authoritative current-source
  // matching is exercised by the actual backend export resolver tests.
  cases[0].film={clip_id:"c-1",focus_seconds:1.25,source_export_job_id:"browser-protocol-export",source_export_sha256:"a".repeat(64)};
  cases[0].film_unavailable_reason=null;
  await mockBackendNoAuth(page, {
    "GET /api/v1/*":[],
    "POST /api/v1/auth/refresh":{access_token:makeE2eJwt("analyst"),refresh_token:"e2e-refresh-token"},
    "GET /api/v1/metrics/definitions":{definitions:[]},
    "POST /api/v1/analytics/query":{dataset_version:"protocol-case-browser-only",cohort:{plays:0,player_reps:0,eligible_measurements:0,excluded_measurements:0},groups:[],exclusions:[]},
    "GET /api/v1/mlops/models":[defaultModel,model],
    "GET /api/v1/learning/evaluations":[{...historical,id:defaultReportId,model_id:defaultModelId},{...historical,id:alternateReportId},producer.evaluation],
    [`GET /api/v1/learning/evaluations/${producer.evaluation.id}/cases`]:linked,
    "GET /api/v1/clips/c-1":sampleClip(),"GET /api/v1/clips/c-1/overlays":sampleOverlays(),
    "GET /api/v1/videos/v-1":sampleVideo(),"GET /api/v1/videos/v-1/clips":[sampleClip()],
    "GET /api/v1/videos/download-url":{downloadUrl:"https://media.e2e.local/protocol-only.mp4"},
  });
  await seedAuthSession(page,"analyst");
  await page.goto("/team-stats/?tab=analyst&layout=analyst&position=QB");
  const reliability=page.getByRole("region",{name:"Model reliability evidence",exact:true});
  await expect(reliability.getByLabel("Reliability model version")).toHaveValue(defaultModelId);
  await reliability.getByLabel("Reliability model version").selectOption(model.id);
  await expect(reliability.getByLabel("Reliability evaluation record")).toHaveValue(alternateReportId);
  await reliability.getByLabel("Reliability evaluation record").selectOption(producer.evaluation.id);
  const diagnostic=page.getByRole("region",{name:"Held-out case diagnostics",exact:true});
  await expect(diagnostic.getByText(/20 of 20 held-out plays/)).toBeVisible();
  await expect(diagnostic.getByLabel("Held-out case dimension")).toHaveValue("");
  const film=diagnostic.getByRole("link",{name:"Watch exact source evidence"});
  const destination=new URL((await film.getAttribute("href"))!,"https://fixture.invalid");
  const back=new URL(destination.searchParams.get("from")!,"https://fixture.invalid");
  expect(back.searchParams.get("reliability_model")).toBe(model.id);
  expect(back.searchParams.get("reliability_evaluation")).toBe(producer.evaluation.id);
  expect(back.searchParams.get("reliability_case_dimension")).toBe("");
  expect(back.searchParams.get("reliability_case_offset")).toBe("0");
  await film.click();
  await expect(page).toHaveURL(/clipId=c-1/);
  await page.getByRole("link",{name:"Back",exact:true}).click();
  await expect(reliability.getByLabel("Reliability model version")).toHaveValue(model.id);
  await expect(reliability.getByLabel("Reliability evaluation record")).toHaveValue(producer.evaluation.id);
  await expect(diagnostic.getByLabel("Held-out case dimension")).toHaveValue("");
  await expect(diagnostic.getByText(/20 of 20 held-out plays/)).toBeVisible();
  expect(new URL(page.url()).searchParams.get("position")).toBe("QB");
  expect(new URL(page.url()).searchParams.get("reliability_case_offset")).toBe("0");
  expect(errors).toEqual([]);
});
