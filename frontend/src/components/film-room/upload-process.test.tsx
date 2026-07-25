import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/image", () => ({ default: () => null }));

const UPLOADED_VIDEO = {
  video_id: "vid-1",
  filename: "practice.mp4",
  video_status: "uploaded",
  total_jobs: 0,
  running_jobs: 0,
  succeeded_jobs: 0,
  failed_jobs: 0,
  clip_count: 0,
  calibration_safe_pct: null,
  latest_error_stage: null,
  latest_error_message: null,
  same_session_job_count: 0,
  pose_pipeline_active: false,
  created_at: "2026-05-30T10:00:00Z",
};

interface MockOptions {
  jobResponse?: { status: number; body: unknown };
  postBodies?: Array<Record<string, unknown>>;
}

function installFetch(opts: MockOptions = {}) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method === "POST" && url.includes("/api/v1/jobs")) {
      opts.postBodies?.push(JSON.parse((init?.body as string) ?? "{}"));
      const r = opts.jobResponse ?? { status: 201, body: { id: "job-1", status: "queued" } };
      return {
        ok: r.status >= 200 && r.status < 300,
        status: r.status,
        json: async () => r.body,
        text: async () => JSON.stringify(r.body),
      } as Response;
    }
    if (url.includes("/api/v1/inbox/status")) {
      return {
        ok: true,
        status: 200,
        json: async () => [UPLOADED_VIDEO],
        text: async () => JSON.stringify([UPLOADED_VIDEO]),
      } as Response;
    }
    // videos / jobs / self-scout / players — empty.
    return {
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function renderUploadProcess() {
  vi.resetModules();
  const [{ UploadProcessFilm }, { AppStateProvider }] = await Promise.all([
    import("./upload-process"),
    import("@/lib/app-state"),
  ]);
  render(
    <AppStateProvider>
      <UploadProcessFilm onUploadClick={() => {}} />
    </AppStateProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
});

describe("UploadProcessFilm", () => {
  test("offline backend explains what is missing instead of faking data", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    await renderUploadProcess();
    await waitFor(() => {
      expect(screen.getByTestId("processing-empty").textContent).toMatch(/Backend offline/i);
    });
  });

  test("uploaded film shows a Process Film CTA (no auto-enqueue)", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetch();
    await renderUploadProcess();
    await waitFor(() => {
      expect(screen.getByTestId("processing-row-vid-1")).toBeTruthy();
    });
    expect(screen.getByTestId("processing-status-vid-1").textContent).toMatch(/Uploaded/i);
    expect(screen.getByTestId("process-film-vid-1")).toBeTruthy();
  });

  test("Process Film posts an ingest job to the backend and reflects Queued", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const postBodies: Array<Record<string, unknown>> = [];
    installFetch({ postBodies });
    await renderUploadProcess();

    const button = await screen.findByTestId("process-film-vid-1");
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByTestId("processing-status-vid-1").textContent).toMatch(/Queued/i);
    });
    // The CTA goes through the sanctioned backend job API with a full
    // orchestrated pipeline job.
    expect(postBodies.length).toBeGreaterThan(0);
    expect(postBodies[0]).toMatchObject({ video_id: "vid-1", job_type: "pipeline" });
    // No longer offering "Process Film" once queued.
    expect(screen.queryByTestId("process-film-vid-1")).toBeNull();
  });

  test("failed film retries the failed job via POST /jobs/{id}/retry", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const FAILED_VIDEO = {
      ...UPLOADED_VIDEO,
      video_id: "vid-2",
      filename: "failed.mp4",
      video_status: "failed",
      failed_jobs: 1,
      latest_error_stage: "track",
      latest_error_message: "tracking crashed",
    };
    const respond = (body: unknown, status = 200) =>
      ({
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
        text: async () => JSON.stringify(body),
      }) as Response;
    const calls: string[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push(`${method} ${url}`);
      if (method === "POST" && url.includes("/api/v1/jobs/job-f1/retry")) {
        return respond({ id: "job-f2", status: "queued" }, 201);
      }
      if (
        method === "GET" &&
        url.includes("/api/v1/jobs") &&
        url.includes("video_id=vid-2") &&
        url.includes("status=failed")
      ) {
        return respond([
          { id: "job-f1", job_type: "pipeline", status: "failed", priority: 0, created_at: "2026-05-30T10:00:00Z" },
        ]);
      }
      if (url.includes("/api/v1/inbox/status")) {
        return respond([FAILED_VIDEO]);
      }
      return respond([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderUploadProcess();

    const button = await screen.findByTestId("retry-process-vid-2");
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByTestId("processing-status-vid-2").textContent).toMatch(/Queued/i);
    });
    // The retry endpoint was used — not a fresh POST /jobs pipeline job.
    expect(calls.some((c) => c.startsWith("POST") && c.includes("/api/v1/jobs/job-f1/retry"))).toBe(true);
    const freshJobPosts = calls.filter(
      (c) => c.startsWith("POST") && c.includes("/api/v1/jobs") && !c.includes("/retry"),
    );
    expect(freshJobPosts).toHaveLength(0);
  });

  test("workload-gated (503) surfaces a coach-readable busy message", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetch({
      jobResponse: {
        status: 503,
        body: { detail: { message: "The GPU queue is saturated. Try again shortly." } },
      },
    });
    await renderUploadProcess();

    const button = await screen.findByTestId("process-film-vid-1");
    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByTestId("processing-busy-vid-1").textContent).toMatch(/saturated/i);
    });
  });
});
