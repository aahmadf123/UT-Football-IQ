// JobRow: per-stage progress folding and lease/attempt surfacing. This
// coverage moved from dashboard.test.tsx when pipeline detail moved to its
// single home (Film Room → Upload & Processing).
import { describe, expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { JobRow, leaseSummary, summarizeProgress } from "./job-row";
import type { ApiJob } from "@/lib/types";

const BASE_JOB: ApiJob = {
  id: "job-1",
  job_type: "pipeline",
  status: "running",
  priority: 0,
  pipeline_mode: "nightly",
  is_same_session: false,
  error_stage: null,
  error_message: null,
  nightly_followup_job_id: null,
  progress: {
    ingest: { status: "succeeded" },
    detect: { status: "succeeded" },
    "track:1a2b3c4d": { status: "started" },
    "pose:1a2b3c4d": { status: "skipped" },
  },
  created_at: "2026-07-20T10:00:00Z",
} as ApiJob;

describe("JobRow", () => {
  test("renders per-stage progress with clip-suffixed stages folded in", () => {
    render(<JobRow job={BASE_JOB} />);
    const progress = screen.getByTestId("job-progress-job-1");
    expect(progress.textContent).toContain("ingest");
    expect(progress.textContent).toContain("detect");
    expect(progress.textContent).toContain("track");
    expect(progress.textContent).toContain("✓"); // succeeded stages
    expect(progress.textContent).toContain("●"); // running stage
    // First-attempt job without lease info shows no extra line.
    expect(screen.queryByTestId("job-lease-job-1")).toBeNull();
  });

  test("retried running job surfaces attempt count + worker id", () => {
    const job = { ...BASE_JOB, id: "job-2", progress: null, attempt_count: 2, leased_by: "gpu-worker-1" };
    render(<JobRow job={job as ApiJob} />);
    expect(screen.getByTestId("job-lease-job-2").textContent).toBe(
      "attempt 2 · worker gpu-worker-1",
    );
  });

  test("failed job surfaces attempts burned and error detail", () => {
    const job = {
      ...BASE_JOB,
      id: "job-3",
      status: "failed",
      progress: null,
      attempt_count: 3,
      leased_by: null,
      error_stage: "track",
      error_message: "CUDA out of memory",
    };
    render(<JobRow job={job as ApiJob} />);
    expect(screen.getByTestId("job-lease-job-3").textContent).toBe("failed after 3 attempts");
    expect(screen.getByText(/track: CUDA out of memory/)).toBeTruthy();
  });
});

describe("leaseSummary", () => {
  test("running first attempt without worker is quiet", () => {
    expect(leaseSummary({ status: "running", attempt_count: 1, leased_by: null } as ApiJob)).toBeNull();
  });
  test("queued jobs are quiet regardless of attempts", () => {
    expect(leaseSummary({ status: "queued", attempt_count: 2, leased_by: null } as ApiJob)).toBeNull();
  });
});

describe("summarizeProgress", () => {
  test("any failed entry marks the stage failed", () => {
    const stages = summarizeProgress({
      "track:a": { status: "succeeded" },
      "track:b": { status: "failed" },
    });
    expect(stages).toEqual([{ stage: "track", status: "failed" }]);
  });
  test("null progress yields no stages", () => {
    expect(summarizeProgress(null)).toEqual([]);
  });
});
