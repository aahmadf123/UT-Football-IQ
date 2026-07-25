import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

beforeEach(() => {
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

describe("fetchMetrics", () => {
  test("requests filtered effort review candidates", async () => {
    const metric = {
      id: "metric-1",
      clip_id: "clip-1",
      tracklet_id: "track-1",
      metric_name: "effort_review_candidate",
      metric_value: { player_id: "player-1" },
      unit: "review",
      is_suppressed: false,
      suppression_reason: null,
      experimental_flag: true,
      analytics_safe: false,
      confidence: 0.8,
      effort_zscore: -2.1,
      loaf_flag: true,
      evidence_uri: null,
      model_version_id: null,
      calibration_version_id: null,
      job_id: null,
      created_at: "2026-05-31T12:00:00Z",
    };
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [metric],
      text: async () => JSON.stringify([metric]),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchMetrics } = await freshImport();
    const metrics = await fetchMetrics(
      { metric_name: "effort_review_candidate", limit: 25 },
      "tok123",
    );

    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/metrics?metric_name=effort_review_candidate&limit=25");
    expect((opts.headers as Record<string, string>).Authorization).toBe("Bearer tok123");
    expect(metrics).toEqual([metric]);
  });
});

describe("createCorrection", () => {
  test("posts an effort flag flip to coach corrections", async () => {
    const response = {
      id: "correction-1",
      clip_id: "clip-1",
      correction_type: "effort_tag",
      original_value: { metric_id: "metric-1" },
      corrected_value: { loaf_flag: false },
      corrected_by: "coach-1",
      notes: null,
      training_eligible: true,
      exported_as_label: false,
      created_at: "2026-05-31T12:00:00Z",
    };
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => response,
      text: async () => JSON.stringify(response),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { createCorrection } = await freshImport();
    await createCorrection(
      {
        clip_id: "clip-1",
        correction_type: "effort_tag",
        original_value: { metric_id: "metric-1" },
        corrected_value: { loaf_flag: false },
      },
      "tok123",
    );

    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/corrections");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(String(opts.body))).toMatchObject({
      clip_id: "clip-1",
      correction_type: "effort_tag",
      corrected_value: { loaf_flag: false },
    });
  });
});

