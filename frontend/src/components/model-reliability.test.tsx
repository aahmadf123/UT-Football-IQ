import { afterEach, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import producer from "./__fixtures__/model-reliability-producer.json";
import type { ModelVersion } from "@/lib/types";

const api = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("@/lib/api", () => ({ listLearningEvaluations: api.list }));
import { ModelReliability } from "./model-reliability";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); api.list.mockReset(); });

function mount(model = producer.model as ModelVersion, reports: unknown[] = [producer.evaluation]) {
  api.list.mockResolvedValue(reports);
  const reload = vi.fn();
  render(<ModelReliability models={[model]} authToken="staff-token" onReloadModels={reload} />);
  return reload;
}

test("actual producer/stored protocol evaluation shows measured diagnostics and failed population gate", async () => {
  expect(producer.fixture_notice).toContain("not real football validation");
  mount();
  await screen.findByText("Holdout gates did not pass; candidate remains experimental.");
  expect(api.list).toHaveBeenCalledWith("staff-token");
  expect(screen.getByText("expected-outcomes-2")).toBeTruthy();
  expect(screen.getByRole("img", { name: "Stored model and baseline calibration bin means" })).toBeTruthy();
  expect(screen.getByText(/Recorded probability decision threshold: 0.5/)).toBeTruthy();
  expect(screen.getAllByRole("meter").length).toBe(producer.evaluation.metrics.error_distribution.bins.length);
  expect(screen.getByText(/requires 240 distinct heldout canonical plays/)).toBeTruthy();
  expect(screen.getByText(/Prediction coverage: Unavailable/)).toBeTruthy();
  expect(screen.queryByText("Human-approved production binding recorded.")).toBeNull();
});

test("historical records keep missing diagnostics unknown and recorded coverage distinct", async () => {
  const legacy = { id: "old", model_id: producer.model.id, source: "measured_human_holdout", artifact_sha256: "a".repeat(64), sample_count: 20, prediction_count: 12, accuracy: 0.5, macro_f1: 0.4, passed: false };
  mount({ ...producer.model, metrics: { evaluation_id: "old", artifact_sha256: "a".repeat(64) } } as ModelVersion, [legacy]);
  await screen.findByText(/Prediction coverage: 12 \/ 20 \(60.0%\)/);
  expect(screen.getByText(/Calibration bins unavailable/)).toBeTruthy();
  expect(screen.getByText(/Error-distribution bins unavailable/)).toBeTruthy();
  expect(screen.getByText(/Confusion counts unavailable/)).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
});

test("small or foreign reports cannot look approved and qualification filter has no invented result", async () => {
  const report = { ...producer.evaluation, passed: true };
  mount({ ...producer.model, promoted_stage: "production", metrics: { ...producer.model.metrics, approved_evaluation_id: report.id, approved_artifact_sha256: report.artifact_sha256, artifact_sha256: "b".repeat(64) } } as ModelVersion, [report]);
  await screen.findByText(/Historical or unbound evaluation/);
  expect(screen.queryByText("Human-approved production binding recorded.")).toBeNull();
  fireEvent.change(screen.getByLabelText("Report qualification"), { target: { value: "passed" } });
  expect(screen.getByText(/No actual stored evaluation matches/)).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
});

test("malformed diagnostic counts refuse charts and never normalize guessed counts", async () => {
  const bad = structuredClone(producer.evaluation);
  bad.metrics.calibration_bins[0].count += 1;
  bad.metrics.error_distribution.bins[0].count = -1;
  bad.metrics.confusion_matrix.counts[0][0] += 1;
  mount(producer.model as ModelVersion, [bad]);
  await screen.findByText(/Calibration bins unavailable/);
  expect(screen.getByText(/Error-distribution bins unavailable/)).toBeTruthy();
  expect(screen.getByText(/Confusion counts unavailable/)).toBeTruthy();
  expect(screen.queryByRole("img")).toBeNull();
});

test("reload reaches authenticated API and download contains the selected stored record", async () => {
  let blob: Blob | undefined;
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn((value: Blob) => { blob = value; return "blob:record"; }), revokeObjectURL: vi.fn() });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  const reload = mount();
  await screen.findByText("Holdout gates did not pass; candidate remains experimental.");
  fireEvent.click(screen.getByRole("button", { name: "Reload model reports" }));
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
  expect(reload).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "Download evaluation record" }));
  expect(click).toHaveBeenCalledOnce();
  expect((click.mock.instances[0] as unknown as HTMLAnchorElement).download).toBe(`model-evaluation-${producer.evaluation.id}.json`);
  const content = await new Promise<string>((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(blob!); });
  expect(JSON.parse(content).stored_evaluation_record).toEqual(producer.evaluation);
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:record"), { timeout: 2000 });
});

test("unknown model evidence stays unknown and failed reload has an explicit state", async () => {
  api.list.mockRejectedValue(new Error("Report service unavailable"));
  render(<ModelReliability models={[]} onReloadModels={vi.fn()} />);
  expect(screen.getByText(/No registered model or actual evaluation report/)).toBeTruthy();
  expect(await screen.findByRole("alert")).toBeTruthy();
});

test("fractional prediction coverage is unavailable in a legacy classification record", async () => {
  const report = { id: "fractional", model_id: producer.model.id, source: "measured_human_holdout", sample_count: 20, prediction_count: 19.5 };
  mount(producer.model as ModelVersion, [report]);
  expect(await screen.findByText(/Prediction coverage: Unavailable/)).toBeTruthy();
  expect(screen.getByText(/recorded classification gates, separate/)).toBeTruthy();
});
