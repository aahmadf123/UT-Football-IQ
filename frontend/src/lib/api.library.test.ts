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

describe("fetchPracticeSessions", () => {
  test("encodes filters as query params", async () => {
    const sessions = [
      {
        practice_session_id: null,
        session_date: "2026-05-20",
        session_kind: "practice",
        opponent_team: null,
        video_count: 2,
        first_recorded_at: "2026-05-20T15:00:00Z",
        last_recorded_at: "2026-05-20T17:00:00Z",
      },
    ];
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => sessions,
      text: async () => JSON.stringify(sessions),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchPracticeSessions } = await freshImport();
    const result = await fetchPracticeSessions({
      recorded_after: "2026-05-20T00:00:00Z",
      recorded_before: "2026-05-20T23:59:59.999999Z",
      session_kind: "practice",
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toContain("https://api.test/api/v1/practice-sessions");
    expect(url).toContain("recorded_after=2026-05-20T00%3A00%3A00Z");
    expect(url).toContain("recorded_before=2026-05-20T23%3A59%3A59.999999Z");
    expect(url).toContain("session_kind=practice");
    expect(result).toEqual(sessions);
  });

  test("omits empty/undefined filters", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchPracticeSessions } = await freshImport();
    await fetchPracticeSessions({});

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.test/api/v1/practice-sessions");
  });
});

describe("fetchVideos / fetchClipsForVideo / fetchClip / fetchVideo", () => {
  test("GETs scoped endpoints with optional filters", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchVideos, fetchClipsForVideo, fetchClip, fetchVideo } = await freshImport();
    await fetchVideos({ session_kind: "game", opponent_team: "NIU" });
    await fetchClipsForVideo("video-1");
    await fetchClip("clip-1");
    await fetchVideo("video-1");

    const urls = fetchMock.mock.calls.map((c) => String((c as unknown[])[0]));
    expect(urls[0]).toBe("https://api.test/api/v1/videos?session_kind=game&opponent_team=NIU");
    expect(urls[1]).toBe("https://api.test/api/v1/videos/video-1/clips");
    expect(urls[2]).toBe("https://api.test/api/v1/clips/clip-1");
    expect(urls[3]).toBe("https://api.test/api/v1/videos/video-1");
  });
});

describe("fetchAlerts", () => {
  test("encodes filter params", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchAlerts } = await freshImport();
    await fetchAlerts({ severity: "warning", acknowledged: false, limit: 25 });

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toContain("/api/v1/alerts");
    expect(url).toContain("severity=warning");
    expect(url).toContain("acknowledged=false");
    expect(url).toContain("limit=25");
  });
});

describe("retryJob", () => {
  test("POSTs to /api/v1/jobs/:id/retry with Authorization header", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => ({ id: "new-job" }),
      text: async () => '{"id":"new-job"}',
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { retryJob } = await freshImport();
    await retryJob("job-1", "tok1");

    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.test/api/v1/jobs/job-1/retry");
    expect(opts.method).toBe("POST");
    expect((opts.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok1");
  });
});

describe("fetchVideoDownloadUrl", () => {
  test("sends bucket + key query params and returns downloadUrl", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ downloadUrl: "https://api.test/api/v1/storage/raw-video/raw/123.mp4?exp=999&sig=abc" }),
      text: async () => '{"downloadUrl":"https://api.test/api/v1/storage/raw-video/raw/123.mp4?exp=999&sig=abc"}',
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchVideoDownloadUrl } = await freshImport();
    const url = await fetchVideoDownloadUrl("raw-video", "raw/123.mp4");
    expect(url).toBe("https://api.test/api/v1/storage/raw-video/raw/123.mp4?exp=999&sig=abc");
    const [reqUrl] = fetchMock.mock.calls[0] as unknown as [string];
    expect(reqUrl).toContain("/api/v1/videos/download-url");
    expect(reqUrl).toContain("bucket=raw-video");
    expect(reqUrl).toContain("key=raw%2F123.mp4");
  });

  test("returns null when the API base is not configured", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { fetchVideoDownloadUrl } = await freshImport();
    const url = await fetchVideoDownloadUrl("raw-video", "raw/test.mp4");
    expect(url).toBeNull();
  });

  test("returns null on non-ok response", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({}),
      text: async () => "{}",
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchVideoDownloadUrl } = await freshImport();
    const url = await fetchVideoDownloadUrl("raw-video", "raw/missing.mp4");
    expect(url).toBeNull();
  });
});

describe("parseStorageUri", () => {
  test("parses s3://raw-video/raw/123-file.mp4", async () => {
    const { parseStorageUri } = await freshImport();
    expect(parseStorageUri("s3://raw-video/raw/123-file.mp4")).toEqual({
      bucket: "raw-video",
      key: "raw/123-file.mp4",
    });
  });

  test("parses s3://clips/processed/abc.mp4", async () => {
    const { parseStorageUri } = await freshImport();
    expect(parseStorageUri("s3://clips/processed/abc.mp4")).toEqual({
      bucket: "clips",
      key: "processed/abc.mp4",
    });
  });

  test("returns null for null / undefined / empty / invalid URIs", async () => {
    const { parseStorageUri } = await freshImport();
    expect(parseStorageUri(null)).toBeNull();
    expect(parseStorageUri(undefined)).toBeNull();
    expect(parseStorageUri("")).toBeNull();
    expect(parseStorageUri("https://example.com/file.mp4")).toBeNull();
  });
});

describe("subscribeAlerts", () => {
  test("issues fetch with Authorization header (no token in URL) and parses SSE events", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            'data: {"type":"connected","connection_id":"abc"}\n\n' +
              'data: {"id":"a1","alert_type":"effort_anomaly","severity":"warning","confidence":0.9,"position_group":"WR","metric_name":"x","metric_value":{},"deviation_sd":null,"clip_uri":null,"period_name":null,"session_id":null,"job_id":null,"is_acknowledged":false,"acknowledged_by":null,"acknowledged_at":null,"player_id":null,"clip_id":null,"created_at":"2026-05-26T00:00:00Z"}\n\n',
          ),
        );
        controller.close();
      },
    });
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return { ok: true, status: 200, body: stream } as unknown as Response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const { subscribeAlerts } = await freshImport();
    const received: unknown[] = [];
    const handle = subscribeAlerts((ev) => received.push(ev), undefined, "tok123");

    // Wait briefly for the async reader to drain the in-memory stream.
    await new Promise((r) => setTimeout(r, 10));

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = calls[0];
    expect(url).toBe("https://api.test/api/v1/alerts/stream");
    expect(url).not.toContain("access_token");
    const headers = init?.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok123");

    expect(received[0]).toEqual({ type: "connected", connection_id: "abc" });
    expect(received[1]).toMatchObject({ type: "alert", alert: { id: "a1" } });

    handle.close();
  });

  test("throws when API URL is not configured", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { subscribeAlerts } = await freshImport();
    expect(() => subscribeAlerts(() => {})).toThrow(/NEXT_PUBLIC_API_URL/);
  });
});
