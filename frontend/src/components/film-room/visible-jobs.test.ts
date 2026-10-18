import { describe, expect, test } from "vitest";
import type { ApiJob } from "@/lib/types";
import { visibleJobs } from "./upload-process";

const job = (id: string, status: string, extra: Partial<ApiJob> = {}): ApiJob => ({
  id,
  job_type: "pipeline",
  status,
  priority: 0,
  video_id: "v1",
  created_at: "2026-10-18T00:00:00Z",
  ...extra,
});

describe("visibleJobs", () => {
  test("hides scheduler maintenance and moves finished jobs to history", () => {
    const { current, history } = visibleJobs([
      job("rollup", "queued", { job_type: "workload_rollup", video_id: undefined }),
      job("q", "queued"),
      job("f", "failed"),
      job("c", "cancelled"),
      job("s", "succeeded"),
    ]);
    expect(current.map((j) => j.id)).toEqual(["q", "f"]);
    expect(history.map((j) => j.id)).toEqual(["c", "s"]);
  });
});
