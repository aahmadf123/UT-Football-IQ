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

const samplePlayer = {
  id: "11111111-1111-1111-1111-111111111111",
  first_name: "Marcus",
  last_name: "Carter",
  jersey_number: 11,
  position: "WR",
  position_group: "Skill",
  is_active: true,
  user_id: null,
  metadata: null,
  created_at: "2026-05-01T12:00:00Z",
};

describe("fetchPlayers", () => {
  test("issues GET /api/v1/players and encodes filters", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [samplePlayer],
      text: async () => JSON.stringify([samplePlayer]),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchPlayers } = await freshImport();
    const players = await fetchPlayers({
      position: "WR",
      position_group: "Skill",
      is_active: true,
      jersey_number: 11,
      search: "carter",
      limit: 25,
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toContain("https://api.test/api/v1/players");
    expect(url).toContain("position=WR");
    expect(url).toContain("position_group=Skill");
    expect(url).toContain("is_active=true");
    expect(url).toContain("jersey_number=11");
    expect(url).toContain("search=carter");
    expect(url).toContain("limit=25");
    expect(players).toEqual([samplePlayer]);
  });

  test("omits empty filters", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchPlayers } = await freshImport();
    await fetchPlayers({});

    const [url] = fetchMock.mock.calls[0] as unknown as [string];
    expect(url).toBe("https://api.test/api/v1/players");
  });

  test("throws when NEXT_PUBLIC_API_URL is not set", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { fetchPlayers } = await freshImport();
    await expect(fetchPlayers()).rejects.toThrow(/NEXT_PUBLIC_API_URL/);
  });

  test("throws on non-ok response", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 500,
      json: async () => ({ error: "boom" }),
      text: async () => '{"error":"boom"}',
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchPlayers } = await freshImport();
    await expect(fetchPlayers()).rejects.toThrow(/500/);
  });
});

describe("fetchPlayer", () => {
  test("issues GET /api/v1/players/:id with bearer token", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => samplePlayer,
      text: async () => JSON.stringify(samplePlayer),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchPlayer } = await freshImport();
    const player = await fetchPlayer(samplePlayer.id, "tok123");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, opts] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`https://api.test/api/v1/players/${samplePlayer.id}`);
    const headers = opts.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok123");
    expect(player).toEqual(samplePlayer);
  });

  test("surfaces 404 in error message", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Player not found" }),
      text: async () => '{"detail":"Player not found"}',
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { fetchPlayer } = await freshImport();
    await expect(fetchPlayer("missing")).rejects.toThrow(/404/);
  });
});

describe("apiPlayerToSummary", () => {
  test("maps identity fields and derives a coaching group", async () => {
    vi.resetModules();
    const { apiPlayerToSummary } = await import("./app-state");
    const summary = apiPlayerToSummary({
      id: "p1",
      first_name: "Marcus",
      last_name: "Carter",
      jersey_number: 11,
      position: "WR",
      position_group: null,
      is_active: true,
      user_id: null,
      metadata: null,
      created_at: "2026-05-01T12:00:00Z",
    });
    expect(summary.id).toBe("p1");
    expect(summary.jersey).toBe("11");
    expect(summary.name).toBe("M. Carter");
    expect(summary.position).toBe("WR");
    // Skill group is derived from WR when the backend hasn't set it.
    expect(summary.group).toBe("Skill");
    // Metrics deliberately not fabricated — they arrive only via
    // mergePlayerMetrics from the live metrics summary endpoint.
    expect(summary.maxSpeed).toBeUndefined();
    expect(summary.distance).toBeUndefined();
    expect(summary.confidence).toBeUndefined();
    expect(summary.identityBucket).toBeUndefined();
    expect(summary.trackedClips).toBeUndefined();
    expect(summary.trend).toBeUndefined();
  });

  test("mergePlayerMetrics fills live aggregates and converts units", async () => {
    vi.resetModules();
    const { apiPlayerToSummary, mergePlayerMetrics } = await import("./app-state");
    const base = apiPlayerToSummary({
      id: "p1",
      first_name: "Marcus",
      last_name: "Carter",
      jersey_number: 11,
      position: "WR",
      position_group: null,
      is_active: true,
      user_id: null,
      metadata: null,
      created_at: "2026-05-01T12:00:00Z",
    });
    const [merged] = mergePlayerMetrics(
      [base],
      [
        {
          player_id: "p1",
          tracklet_count: 4,
          tracked_clip_count: 3,
          last_tracked_at: "2026-08-10T19:00:00Z",
          identity_confidence: 0.84,
          identity_bucket: "probable",
          max_speed_yps: 8.5,
          max_speed_samples: 4,
          distance_yards: 123.4,
          distance_samples: 4,
        },
      ],
    );
    // 8.5 yd/s ≈ 17.4 mph.
    expect(merged.maxSpeed).toBeCloseTo(17.4, 1);
    expect(merged.distance).toBe(123);
    expect(merged.confidence).toBe(0.84);
    expect(merged.identityBucket).toBe("probable");
    expect(merged.trackedClips).toBe(3);
    // A player absent from the metrics rows keeps its honest undefineds.
    const [untouched] = mergePlayerMetrics([base], []);
    expect(untouched.maxSpeed).toBeUndefined();
  });

  test("prefers the backend position_group over derivation", async () => {
    vi.resetModules();
    const { apiPlayerToSummary } = await import("./app-state");
    const summary = apiPlayerToSummary({
      id: "p2",
      first_name: "Adam",
      last_name: "Moore",
      jersey_number: 9,
      position: "QB",
      position_group: "Custom Group",
      is_active: true,
      user_id: null,
      metadata: null,
      created_at: "2026-05-01T12:00:00Z",
    });
    expect(summary.group).toBe("Custom Group");
  });

  test("falls back to Unassigned when nothing is set", async () => {
    vi.resetModules();
    const { apiPlayerToSummary } = await import("./app-state");
    const summary = apiPlayerToSummary({
      id: "p3",
      first_name: "Jane",
      last_name: "Doe",
      jersey_number: null,
      position: null,
      position_group: null,
      is_active: false,
      user_id: null,
      metadata: null,
      created_at: "2026-05-01T12:00:00Z",
    });
    expect(summary.jersey).toBe("—");
    expect(summary.position).toBe("Unassigned");
    expect(summary.group).toBe("Unassigned");
  });
});
