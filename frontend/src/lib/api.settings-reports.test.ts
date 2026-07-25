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

function mockResponse(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

describe("settings api helpers", () => {
  test("getSystemSettings GETs /api/v1/settings/system with bearer token", async () => {
    const payload = {
      system_config: {
        team_name: "Toledo Rockets",
        capture_camera: "Drone (primary)",
        storage_bucket: "artifacts",
        auto_export_access: "staff",
      },
      model_sensitivity: {
        boundary_sensitivity: 68,
        identity_confidence: 82,
        motion_minimum: 72,
        pose_review_gate: 88,
      },
    };
    const fetchMock = vi.fn(async () => mockResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const { getSystemSettings } = await freshImport();
    const result = await getSystemSettings("tok123");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/settings/system");
    expect((opts.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok123");
    expect(result).toEqual(payload);
  });

  test("updateSystemSettings PATCHes with JSON body", async () => {
    const payload = {
      system_config: {
        team_name: "Toledo Rockets 2",
        capture_camera: "Drone (primary)",
        storage_bucket: "artifacts",
        auto_export_access: "staff",
      },
      model_sensitivity: {
        boundary_sensitivity: 91,
        identity_confidence: 82,
        motion_minimum: 72,
        pose_review_gate: 88,
      },
    };
    const fetchMock = vi.fn(async () => mockResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const { updateSystemSettings } = await freshImport();
    const result = await updateSystemSettings(
      { system_config: { team_name: "Toledo Rockets 2" } },
      "tok",
    );

    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/settings/system");
    expect(opts.method).toBe("PATCH");
    expect(JSON.parse(opts.body as string)).toEqual({
      system_config: { team_name: "Toledo Rockets 2" },
    });
    expect(result).toEqual(payload);
  });

  test("updateSystemSettings surfaces non-ok response", async () => {
    const fetchMock = vi.fn(async () =>
      mockResponse({ detail: "forbidden" }, { ok: false, status: 403 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { updateSystemSettings } = await freshImport();
    await expect(updateSystemSettings({ system_config: {} }, "tok")).rejects.toThrow(/403/);
  });

  test("getUserPreferences hits /api/v1/settings/me", async () => {
    const payload = {
      theme: "dark",
      default_session_kind: "all",
      default_side_of_ball: "all",
    };
    const fetchMock = vi.fn(async () => mockResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const { getUserPreferences } = await freshImport();
    const result = await getUserPreferences("tok");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.test/api/v1/settings/me");
    expect(result).toEqual(payload);
  });
});

describe("reports api helpers", () => {
  test("listReports GETs /api/v1/reports", async () => {
    const fetchMock = vi.fn(async () => mockResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    const { listReports } = await freshImport();
    const result = await listReports("tok");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.test/api/v1/reports");
    expect(result).toEqual([]);
  });

  test("createReport POSTs the requested body and returns the queued job", async () => {
    const job = {
      id: "job-1",
      report_type: "coaching_summary",
      format: "pdf",
      status: "queued",
      parameters: { sections: ["Self-scout exposure"] },
      output_uri: null,
      error_message: null,
      requested_by: "user-1",
      started_at: null,
      finished_at: null,
      created_at: "2026-05-28T00:00:00Z",
    };
    const fetchMock = vi.fn(async () => mockResponse(job, { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    const { createReport } = await freshImport();
    const result = await createReport(
      { format: "pdf", sections: ["Self-scout exposure"] },
      "tok",
    );

    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/reports");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body as string)).toEqual({
      format: "pdf",
      sections: ["Self-scout exposure"],
    });
    expect(result).toEqual(job);
  });

  test("getReportDownloadUrl returns the presigned response", async () => {
    const body = { download_url: "https://storage.test/abc.pdf", expires_at: "2026-05-28T01:00:00Z" };
    const fetchMock = vi.fn(async () => mockResponse(body));
    vi.stubGlobal("fetch", fetchMock);

    const { getReportDownloadUrl } = await freshImport();
    const result = await getReportDownloadUrl("rep-1", "tok");

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.test/api/v1/reports/rep-1/download");
    expect(result).toEqual(body);
  });
});
