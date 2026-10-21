import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import producer from "./__fixtures__/model-reliability-producer.json";

const fixture = vi.hoisted(() => ({ role: "analyst" }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "staff-token", currentRole: fixture.role }) }));
vi.mock("./learning-workflow", () => ({ LearningWorkflow: () => null }));
vi.mock("@/lib/api", () => ({
  listTrainingDatasets: vi.fn(async () => []), listModelVersions: vi.fn(async () => [producer.model]),
  listLearningBenchmarks: vi.fn(async () => []), listLearningEvaluations: vi.fn(async () => [producer.evaluation]),
  exportCorrections: vi.fn(), getTrainingDatasetDownloadUrl: vi.fn(), promoteModelVersion: vi.fn(),
}));
import { LearningPanel } from "./learning-panel";

afterEach(() => { cleanup(); fixture.role = "analyst"; });

test("staff Learning screen loads the registered model and stored reliability report", async () => {
  render(<LearningPanel />);
  expect(await screen.findByText("Holdout gates did not pass; candidate remains experimental.")).toBeTruthy();
  expect(screen.getByRole("region", { name: "Model reliability evidence" })).toBeTruthy();
  expect(screen.getByRole("img", { name: "Stored model and baseline calibration bin means" })).toBeTruthy();
});

test("nonstaff Learning screen does not expose model reliability controls", () => {
  fixture.role = "coach";
  render(<LearningPanel />);
  expect(screen.queryByRole("region", { name: "Model reliability evidence" })).toBeNull();
});
