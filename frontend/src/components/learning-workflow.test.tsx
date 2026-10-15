import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const fixtures = vi.hoisted(() => ({
  history: [] as unknown[], jobs: [] as unknown[], candidates: [] as unknown[],
  preview: vi.fn(), create: vi.fn(), queue: vi.fn(), evaluate: vi.fn(),
  manifest: { id: "manifest-1", name: "Coverage set", training_dataset_id: "dataset-1", features_uri: "local://artifacts/features/manifest-1.json", labels_uri: "local://artifacts/labels/manifest-1.jsonl", benchmark_id: "benchmark-1", counts: { train: 30, calibration: 10, holdout: 20 }, job_ids: [] },
}));

vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "analyst-token", data: { jobs: fixtures.jobs } }) }));
vi.mock("@/lib/api", () => ({
  fetchSessions: vi.fn(async () => ["Training film", "Calibration film", "Holdout film"].map((title, index) => ({ id: `s-${index + 1}`, title, recordings: [], library_role: "toledo", recorded_at: "2026-10-14T12:00:00Z" }))),
  listFeatureManifests: vi.fn(async () => fixtures.history),
  listLearningCandidates: vi.fn(async () => fixtures.candidates),
  previewFeatureManifest: fixtures.preview, createFeatureManifest: fixtures.create, queueCoverageTraining: fixtures.queue, evaluateLearningCandidate: fixtures.evaluate,
}));

import { LearningWorkflow } from "./learning-workflow";

beforeEach(() => { fixtures.history = []; fixtures.jobs = []; fixtures.candidates = []; });
afterEach(() => { cleanup(); vi.clearAllMocks(); });

async function chooseSplits() {
  await waitFor(() => expect(screen.getByLabelText("Learning split for Training film")).toBeTruthy());
  fireEvent.change(screen.getByLabelText("Learning split for Training film"), { target: { value: "train" } });
  fireEvent.change(screen.getByLabelText("Learning split for Calibration film"), { target: { value: "calibration" } });
  fireEvent.change(screen.getByLabelText("Learning split for Holdout film"), { target: { value: "holdout" } });
}

describe("local coverage learning workflow", () => {
  test("insufficient reviewed film cannot create a feature set or queue training", async () => {
    fixtures.preview.mockResolvedValueOnce({ ready: false, counts: { train: 2, calibration: 0, holdout: 0 }, required: { train: 30, calibration: 10, holdout: 20 }, class_counts: { train: { cover_3: 2 } }, issues: ["More reviewed human coverage labels are required"], job_ids: [] });
    render(<LearningWorkflow onUpdated={vi.fn()} />);
    await chooseSplits();
    fireEvent.click(screen.getByRole("button", { name: "Preview training readiness" }));
    await waitFor(() => expect(screen.getByText("More reviewed human coverage labels are required")).toBeTruthy());
    expect((screen.getByRole("button", { name: "Build feature set" }) as HTMLButtonElement).disabled).toBe(true);
    expect(fixtures.create).not.toHaveBeenCalled();
    expect(fixtures.queue).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Coverage examples per class"));
    expect(screen.getByRole("table", { name: "Reviewed coverage examples by training group" })).toBeTruthy();
    expect(screen.getByRole("row", { name: "Cover 3 2 0 0" })).toBeTruthy();
  });

  test("eligible session splits queue training using saved artifacts and block duplicate clicks", async () => {
    fixtures.preview.mockResolvedValueOnce({ ready: true, counts: fixtures.manifest.counts, required: fixtures.manifest.counts, class_counts: {}, issues: [], job_ids: [] });
    fixtures.create.mockResolvedValueOnce(fixtures.manifest);
    fixtures.queue.mockResolvedValueOnce({ job_id: "train-1", status: "queued" });
    render(<LearningWorkflow onUpdated={vi.fn()} />);
    await chooseSplits();
    fireEvent.click(screen.getByRole("button", { name: "Preview training readiness" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Build feature set" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Build feature set" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Train CPU coverage model" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Train CPU coverage model" }));
    await waitFor(() => expect(fixtures.queue).toHaveBeenCalledWith({ training_dataset_id: "dataset-1", features_uri: "local://artifacts/features/manifest-1.json" }, "analyst-token"));
    expect((screen.getByRole("button", { name: "Train CPU coverage model" }) as HTMLButtonElement).disabled).toBe(true);
    expect(fixtures.create.mock.calls[0][0].sessions).toEqual([{ session_id: "s-1", split: "train" }, { session_id: "s-2", split: "calibration" }, { session_id: "s-3", split: "holdout" }]);
  });

  test("saved feature sets recover running jobs and evaluate real candidate predictions", async () => {
    fixtures.history = [fixtures.manifest];
    fixtures.jobs = [{ id: "train-1", job_type: "train", status: "running", input_artifacts: { training_dataset_id: "dataset-1" } }];
    fixtures.candidates = [{ id: "model-1", model_name: "coverage", version: "1", prediction_count: 20, required_predictions: 20, ready_for_evaluation: true }];
    fixtures.evaluate.mockResolvedValueOnce({ passed: true, accuracy: 0.95, macro_f1: 0.93, sample_count: 20, prediction_count: 20 });
    render(<LearningWorkflow onUpdated={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Saved feature set")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("Saved feature set"), { target: { value: "manifest-1" } });
    expect((screen.getByRole("button", { name: "Train CPU coverage model" }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect((screen.getByRole("button", { name: "Evaluate held-out film" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Evaluate held-out film" }));
    await waitFor(() => expect(fixtures.evaluate).toHaveBeenCalledWith("model-1", "benchmark-1", "analyst-token"));
    await waitFor(() => expect(screen.getByText("Release thresholds passed")).toBeTruthy());
  });
});
