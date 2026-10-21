import { afterEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const fixtures = vi.hoisted(() => ({ jobs: [] as unknown[], queue: vi.fn(), evaluate: vi.fn() }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "analyst-token", data: { jobs: fixtures.jobs } }) }));
vi.mock("@/lib/api", () => ({ queueExpectedOutcomesTraining: fixtures.queue, evaluateExpectedOutcomesCandidate: fixtures.evaluate, getExpectedFeatureDownload: vi.fn() }));

import { ExpectedOutcomesTraining } from "./expected-outcomes-training";

afterEach(() => { cleanup(); fixtures.jobs = []; vi.clearAllMocks(); });

function fill() {
  fireEvent.change(screen.getByLabelText("Reviewed manifest file"), { target: { value: "C:/reviewed/manifest.json" } });
  fireEvent.change(screen.getByLabelText("Manifest SHA-256"), { target: { value: "a".repeat(64) } });
  fireEvent.change(screen.getByLabelText("New candidate folder"), { target: { value: "C:/candidates/new" } });
}

describe("manual expected-outcome training", () => {
  test("requires a manifest checksum and fresh folder before queueing", () => {
    render(<ExpectedOutcomesTraining onUpdated={vi.fn()} />);
    expect((screen.getByRole("button", { name: "Queue expected-outcome training" }) as HTMLButtonElement).disabled).toBe(true);
    fill();
    fireEvent.change(screen.getByLabelText("Manifest SHA-256"), { target: { value: "bad" } });
    expect((screen.getByRole("button", { name: "Queue expected-outcome training" }) as HTMLButtonElement).disabled).toBe(true);
    expect(fixtures.queue).not.toHaveBeenCalled();
  });

  test("explicit staff action queues the selected experimental task and blocks repeat clicks", async () => {
    fixtures.queue.mockResolvedValueOnce({ job_id: "expected-job", status: "queued" });
    render(<ExpectedOutcomesTraining onUpdated={vi.fn()} />);
    fill();
    fireEvent.change(screen.getByLabelText("Expected outcome"), { target: { value: "yac" } });
    fireEvent.click(screen.getByRole("button", { name: "Queue expected-outcome training" }));
    await waitFor(() => expect(fixtures.queue).toHaveBeenCalledWith({ manifest_path: "C:/reviewed/manifest.json", manifest_sha256: "a".repeat(64), output_dir: "C:/candidates/new", task: "yac" }, "analyst-token"));
    expect((screen.getByRole("button", { name: "Queue expected-outcome training" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Candidates remain experimental/)).toBeTruthy();
  });

  test("recovers the queued workflow and displays actual held-out outcome losses", async () => {
    fixtures.jobs = [{ id: "expected-job", job_type: "train", status: "succeeded", input_artifacts: { training_variant: "expected_outcomes" }, output_artifacts: { models: { yac: { report: { count: 20, metrics: { mae: 2.1, rmse: 3.2 }, baseline: { metrics: { mae: 2.8, rmse: 4.1 } } } } } } }];
    render(<ExpectedOutcomesTraining onUpdated={vi.fn()} />);
    expect(screen.getByRole("table", { name: "Expected-outcome holdout evaluation" })).toBeTruthy();
    expect(screen.getByText("2.100 / 2.800")).toBeTruthy();
    expect(screen.queryByText(/accuracy/i)).toBeNull();
    fill();
    expect(screen.getByText(/Recent training request/)).toBeTruthy();
    expect(screen.queryByText(/Selected training request/)).toBeNull();
  });

  test("server verification is separate from human promotion", async () => {
    fixtures.jobs = [{ id: "job", job_type: "train", status: "succeeded", input_artifacts: { training_variant: "expected_outcomes" }, output_artifacts: { registered: { yac: { model_version_id: "candidate" } } } }];
    fixtures.evaluate.mockResolvedValueOnce({ passed: true, sample_count: 20 });
    render(<ExpectedOutcomesTraining onUpdated={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Verify Yards after catch holdout" }));
    await waitFor(() => expect(fixtures.evaluate).toHaveBeenCalledWith("candidate", "analyst-token"));
    expect(screen.getByText("Holdout gate passed; admin model promotion is required.")).toBeTruthy();
  });

  test("queued independent verification is pending rather than a failed evaluation", async () => {
    fixtures.jobs = [{ id: "job", job_type: "train", status: "succeeded", input_artifacts: { training_variant: "expected_outcomes" }, output_artifacts: { registered: { yac: { model_version_id: "candidate" } } } }];
    fixtures.evaluate.mockResolvedValueOnce({ status: "queued", job_id: "verifier", challenge_id: "challenge" });
    render(<ExpectedOutcomesTraining onUpdated={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Verify Yards after catch holdout" }));
    await waitFor(() => expect(screen.getByText("Independent verification: queued")).toBeTruthy());
    expect(screen.queryByText(/Holdout gate did not pass/)).toBeNull();
    expect((screen.getByRole("button", { name: "Verify Yards after catch holdout" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
