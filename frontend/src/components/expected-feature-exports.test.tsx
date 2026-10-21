import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

const fixture = vi.hoisted(() => ({ jobs: [] as unknown[], videos: [{ id: "video", filename: "Reviewed source.mp4" }], get: vi.fn() }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "staff-token", data: { jobs: fixture.jobs, videos: fixture.videos } }) }));
vi.mock("@/lib/api", () => ({ getExpectedFeatureDownload: fixture.get }));
import { ExpectedFeatureExports } from "./expected-feature-exports";

afterEach(() => { cleanup(); fixture.jobs = []; vi.clearAllMocks(); });

test("actual pipeline export gets an authenticated signed download and hash-bound blank-target command", async () => {
  fixture.jobs = [{ id: "source-job", video_id: "video", job_type: "pipeline", status: "succeeded", output_artifacts: { expected_feature_export: { row_count: 3 } } }];
  fixture.get.mockResolvedValue({ download_url: "https://owned.test/download?signed=true", sha256: "a".repeat(64), row_count: 3, size_bytes: 4096, filename: "expected-features.json" });
  render(<ExpectedFeatureExports />);
  fireEvent.click(screen.getByRole("button", { name: "Get feature download" }));
  await waitFor(() => expect(fixture.get).toHaveBeenCalledWith("source-job", "staff-token"));
  expect((await screen.findByRole("link", { name: "Download feature snapshots" })).getAttribute("href")).toBe("https://owned.test/download?signed=true");
  expect(screen.getByText(/Reviewed source.mp4.*3 snapshots/)).toBeTruthy();
  expect(screen.getByText(/python -m training.expected_outcomes targets-template/).textContent).toContain("--snapshots-sha256 " + "a".repeat(64));
  expect(screen.getByText(/target template starts blank/)).toBeTruthy();
});

test("a private checkpoint or failed job does not advertise a usable export", () => {
  fixture.jobs = [{ id: "failed", job_type: "pipeline", status: "failed", output_artifacts: { expected_feature_export: {} } }, { id: "private", job_type: "pipeline", status: "succeeded", output_artifacts: { checkpoint: { feature_snapshots: {} } } }];
  render(<ExpectedFeatureExports />);
  expect(screen.getByText(/No qualified prediction-time feature exports/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Get feature download" })).toBeNull();
});
