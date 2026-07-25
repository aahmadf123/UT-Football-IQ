import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_WORKER_URL", "https://worker.test");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

async function freshImport() {
  vi.resetModules();
  return import("./api");
}

describe("requestUploadUrl", () => {
  test("requests upload URL from configured Worker and returns uploadUrl + key", async () => {
    const mockResponse = { uploadUrl: "https://worker.test/api/v1/videos/upload/raw/123-test.mp4", key: "raw/123-test.mp4" };
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => mockResponse,
      text: async () => JSON.stringify(mockResponse),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { requestUploadUrl } = await freshImport();
    const result = await requestUploadUrl("test.mp4", "tok123");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://worker.test/api/v1/videos/upload-url");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body as string)).toEqual({ filename: "test.mp4" });
    expect((opts.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok123");
    expect(result).toEqual(mockResponse);
  });

  test("falls back to backend when Worker rejects the token", async () => {
    const workerResponse = { error: "Authentication failed" };
    const apiResponse = { uploadUrl: "https://api.test/api/v1/videos/upload/raw/x", key: "raw/x" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => workerResponse,
        text: async () => JSON.stringify(workerResponse),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => apiResponse,
        text: async () => JSON.stringify(apiResponse),
      });
    vi.stubGlobal("fetch", fetchMock);

    const { requestUploadUrl } = await freshImport();
    const result = await requestUploadUrl("test.mp4", "tok123");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe("https://worker.test/api/v1/videos/upload-url");
    expect(fetchMock.mock.calls[1][0]).toBe("https://api.test/api/v1/videos/upload-url");
    expect(result).toEqual(apiResponse);
  });

  test("throws on non-ok response", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ error: "bad" }),
      text: async () => '{"error":"bad"}',
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { requestUploadUrl } = await freshImport();
    await expect(requestUploadUrl("test.mp4")).rejects.toThrow(/400/);
  });

  test("falls back to the API base when NEXT_PUBLIC_WORKER_URL is not set", async () => {
    // Local / single-box mode: the backend mirrors the Worker's upload
    // contract, so an empty worker URL routes upload calls to the API base.
    vi.stubEnv("NEXT_PUBLIC_WORKER_URL", "");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ uploadUrl: "https://api.test/api/v1/videos/upload/raw%2Fx", key: "raw/x" }),
      text: async () => "",
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { requestUploadUrl } = await freshImport();
    await requestUploadUrl("test.mp4");
    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.test/api/v1/videos/upload-url");
  });

  test("throws naming both env vars when neither worker nor API base is configured", async () => {
    vi.stubEnv("NEXT_PUBLIC_WORKER_URL", "");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { requestUploadUrl } = await freshImport();
    // The message must name both vars — with the API-base fallback, requiring
    // only NEXT_PUBLIC_WORKER_URL would be misleading.
    await expect(requestUploadUrl("test.mp4")).rejects.toThrow(/NEXT_PUBLIC_API_URL/);
    await expect(requestUploadUrl("test.mp4")).rejects.toThrow(/NEXT_PUBLIC_WORKER_URL/);
  });
});

describe("uploadToR2", () => {
  test("PUTs file to given URL and returns R2UploadResult", async () => {
    const r2Result = { key: "raw/123-test.mp4", size: 1024, etag: "abc", storageUri: "r2://raw-video/raw/123-test.mp4" };
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => r2Result,
      text: async () => JSON.stringify(r2Result),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { uploadToR2 } = await freshImport();
    const file = new File(["test content"], "test.mp4", { type: "video/mp4" });
    const result = await uploadToR2("https://worker.test/api/v1/videos/upload/raw/123-test.mp4", file);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://worker.test/api/v1/videos/upload/raw/123-test.mp4");
    expect(opts.method).toBe("PUT");
    expect(result).toEqual(r2Result);
  });

  test("throws on upload failure", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 500,
      json: async () => ({ error: "internal" }),
      text: async () => '{"error":"internal"}',
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { uploadToR2 } = await freshImport();
    const file = new File(["test content"], "test.mp4", { type: "video/mp4" });
    await expect(uploadToR2("https://worker.test/upload", file)).rejects.toThrow(/500/);
  });
});

describe("registerVideo", () => {
  test("POSTs to backend /api/v1/videos with metadata", async () => {
    const videoResponse = { id: "vid-1", filename: "test.mp4", status: "uploaded", created_at: "2025-01-01T00:00:00Z" };
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => videoResponse,
      text: async () => JSON.stringify(videoResponse),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { registerVideo } = await freshImport();
    const result = await registerVideo({
      filename: "test.mp4",
      storage_uri: "r2://raw-video/raw/123-test.mp4",
      session_kind: "practice",
      source_type: "drone",
    }, "tok123");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/videos");
    expect(opts.method).toBe("POST");
    const body = JSON.parse(opts.body as string);
    expect(body.filename).toBe("test.mp4");
    expect(body.storage_uri).toBe("r2://raw-video/raw/123-test.mp4");
    expect(body.session_kind).toBe("practice");
    expect(result.id).toBe("vid-1");
  });

  test("throws when NEXT_PUBLIC_API_URL is not set", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { registerVideo } = await freshImport();
    await expect(registerVideo({ filename: "x.mp4", storage_uri: "r2://test" })).rejects.toThrow(/NEXT_PUBLIC_API_URL/);
  });
});

describe("fetchClipOverlays", () => {
  test("GETs /api/v1/clips/:id/overlays with auth header", async () => {
    const payload = {
      clip_id: "c-1",
      tracklets: [],
      events: [],
      labels: [],
      metrics: [],
      layers_available: { tracklets: false, events: false, labels: false, metrics: false },
    };
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchClipOverlays } = await freshImport();
    const result = await fetchClipOverlays("c-1", "tok123");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/clips/c-1/overlays");
    expect((opts.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok123");
    expect(result.clip_id).toBe("c-1");
  });

  test("throws when NEXT_PUBLIC_API_URL is not set", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { fetchClipOverlays } = await freshImport();
    await expect(fetchClipOverlays("c-1")).rejects.toThrow(/NEXT_PUBLIC_API_URL/);
  });
});

describe("requestVideoProcessing", () => {
  test("POSTs a pipeline job to backend /api/v1/jobs (nightly by default)", async () => {
    const job = { id: "job-1", status: "queued" };
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => job,
      text: async () => JSON.stringify(job),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { requestVideoProcessing } = await freshImport();
    const result = await requestVideoProcessing("vid-1", undefined, "tok123");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/jobs");
    expect(opts.method).toBe("POST");
    const body = JSON.parse(opts.body as string);
    expect(body).toMatchObject({
      video_id: "vid-1",
      job_type: "pipeline",
      priority: 0,
      pipeline_mode: "nightly",
    });
    expect((opts.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok123");
    expect(result).toEqual({ jobId: "job-1", status: "queued" });
  });

  test("same_session mode uses priority 10", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => ({ id: "job-2", status: "queued" }),
      text: async () => "{}",
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { requestVideoProcessing } = await freshImport();
    await requestVideoProcessing("vid-1", { mode: "same_session" });

    const [, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(opts.body as string);
    expect(body.priority).toBe(10);
    expect(body.pipeline_mode).toBe("same_session");
  });

  test("503 workload_gated throws a WorkloadGatedError carrying the detail message", async () => {
    const detail = { detail: { message: "Queue saturated, try again." } };
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 503,
      json: async () => detail,
      text: async () => JSON.stringify(detail),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { requestVideoProcessing, WorkloadGatedError } = await freshImport();
    await expect(requestVideoProcessing("vid-1")).rejects.toBeInstanceOf(WorkloadGatedError);
  });

  test("throws when NEXT_PUBLIC_API_URL is not set", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { requestVideoProcessing } = await freshImport();
    await expect(requestVideoProcessing("vid-1")).rejects.toThrow(/NEXT_PUBLIC_API_URL/);
  });
});

describe("fetchInboxStatus", () => {
  test("GETs /api/v1/inbox/status", async () => {
    const items = [{ video_id: "v1", filename: "a.mp4", video_status: "processing", total_jobs: 2, running_jobs: 1, succeeded_jobs: 1, failed_jobs: 0, clip_count: 3, calibration_safe_pct: 80, latest_error_stage: null, latest_error_message: null, same_session_job_count: 1, pose_pipeline_active: false, created_at: "2025-01-01" }];
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => items,
      text: async () => JSON.stringify(items),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchInboxStatus } = await freshImport();
    const result = await fetchInboxStatus("tok123", true);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/v1/inbox/status");
    expect(url).toContain("include_succeeded=true");
    expect(result).toHaveLength(1);
    expect(result[0].video_id).toBe("v1");
  });
});
