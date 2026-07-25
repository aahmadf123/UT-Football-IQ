import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("next/image", () => ({
  default: () => null,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/analytics",
}));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
});

async function importPage() {
  vi.resetModules();
  const [{ default: AnalyticsPage }, { AppStateProvider }] = await Promise.all([
    import("./page"),
    import("@/lib/app-state"),
  ]);
  return { AnalyticsPage, AppStateProvider };
}

interface RouteSpec {
  url: string;
  body: unknown;
  ok?: boolean;
  status?: number;
}

function installFetchRoutes(routes: RouteSpec[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const r of routes) {
      if (url.includes(r.url)) {
        const ok = r.ok ?? true;
        return {
          ok,
          status: r.status ?? (ok ? 200 : 500),
          json: async () => r.body,
          text: async () => JSON.stringify(r.body),
        } as unknown as Response;
      }
    }
    return {
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const SAMPLE_TENDENCIES = {
  formation_tendencies: [
    {
      grouping_key: "Trips Right",
      total_plays: 10,
      run_count: 4,
      pass_count: 6,
      run_rate: 0.4,
      pass_rate: 0.6,
      evidence_clip_ids: [],
      low_sample: false,
    },
  ],
  motion_tendencies: {
    with_motion: { total: 0, run_count: 0, pass_count: 0, run_rate: 0, pass_rate: 0 },
    without_motion: { total: 0, run_count: 0, pass_count: 0, run_rate: 0, pass_rate: 0 },
  },
  field_zone_tendencies: [],
  personnel_tendencies: [],
  down_distance_tendencies: [],
  formation_concept_families: {},
  pre_snap_tells: [],
  alerts: [],
  clip_count: 10,
};

const SAMPLE_ALERT = {
  id: "a-1",
  player_id: null,
  clip_id: null,
  position_group: "WR",
  alert_type: "effort_anomaly",
  severity: "warning",
  confidence: 0.9,
  metric_name: "max_speed",
  metric_value: { value: 18 },
  deviation_sd: null,
  clip_uri: null,
  period_name: null,
  session_id: null,
  job_id: null,
  is_acknowledged: false,
  acknowledged_by: null,
  acknowledged_at: null,
  created_at: "2026-05-26T12:00:00Z",
};

describe("AnalyticsPage card states", () => {
  test("renders xSep/xYards/xPressure as 'unavailable' (not as fake live values)", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes([
      { url: "/api/v1/self-scout/tendencies", body: SAMPLE_TENDENCIES },
      { url: "/api/v1/alerts", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
    ]);

    const { AnalyticsPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <AnalyticsPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("analytics-card-expected-separation-xsep")).toBeTruthy();
    });
    expect(
      screen
        .getByTestId("analytics-card-expected-separation-xsep")
        .getAttribute("data-card-state"),
    ).toBe("unavailable");
    expect(
      screen
        .getByTestId("analytics-card-expected-yards-xyards")
        .getAttribute("data-card-state"),
    ).toBe("unavailable");
    expect(
      screen
        .getByTestId("analytics-card-expected-pressure-xpressure")
        .getAttribute("data-card-state"),
    ).toBe("unavailable");

    // Model Quality / Spatial Heatmap are no longer permanently-empty cards —
    // they live in one honest "In development" footnote.
    expect(screen.queryByTestId("analytics-card-model-quality")).toBeNull();
    expect(screen.queryByTestId("analytics-card-spatial-heatmap")).toBeNull();
    const footnote = screen.getByTestId("analytics-in-development");
    expect(footnote.textContent).toContain("Model Quality");
    expect(footnote.textContent).toContain("Spatial Heatmap");

    // No hardcoded xSep="2.64" / xYards="11.8" / xPressure="95%" must appear.
    expect(screen.queryByText("2.64")).toBeNull();
    expect(screen.queryByText("11.8")).toBeNull();
    expect(screen.queryByText("95%")).toBeNull();
  });

  test("renders frontier metrics as EXPERIMENTAL (never trusted) when backend has samples", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes([
      { url: "/api/v1/self-scout/tendencies", body: SAMPLE_TENDENCIES },
      { url: "/api/v1/alerts", body: [] },
      {
        url: "/api/v1/analytics/frontier",
        body: {
          metrics: [
            {
              id: "m-1",
              clip_id: "c-1",
              tracklet_id: "t-1",
              metric_name: "xsep",
              metric_value: { yards: 2.6, source: "tracking", sample_size: 14 },
              unit: "yd",
              experimental: true,
              analytics_safe: false,
              confidence: 0.6,
              source: "tracking",
              sample_size: 14,
              stability_note: "directional until ~30 reps",
              position_group: "WR",
              created_at: "2026-05-26T12:00:00Z",
            },
          ],
          total: 1,
          experimental: true,
          definitions: {},
          note: "Experimental frontier analytics — NOT validated.",
        },
      },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
    ]);

    const { AnalyticsPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <AnalyticsPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("frontier-xsep")).toBeTruthy();
    });
    // xSep card is now live, with an experimental badge — never presented as trusted.
    expect(
      screen
        .getByTestId("analytics-card-expected-separation-xsep")
        .getAttribute("data-card-state"),
    ).toBe("live");
    expect(screen.getAllByTestId("experimental-badge").length).toBeGreaterThan(0);
    expect(screen.getByText("2.60 yd")).toBeTruthy();
    // xYards / xPressure remain unavailable (no samples) — not fabricated.
    expect(
      screen
        .getByTestId("analytics-card-expected-yards-xyards")
        .getAttribute("data-card-state"),
    ).toBe("unavailable");
  });

  test("renders live formation tendencies when backend returns data; alerts live on /alerts", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes([
      { url: "/api/v1/self-scout/tendencies", body: SAMPLE_TENDENCIES },
      { url: "/api/v1/alerts", body: [SAMPLE_ALERT] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
    ]);

    const { AnalyticsPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <AnalyticsPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText("Trips Right")).toBeTruthy();
    });
    // The alerts feed has one home (/alerts + dashboard summary) — no
    // duplicate card here anymore.
    expect(screen.queryByTestId("analytics-card-coaching-alerts")).toBeNull();
    expect(screen.queryByTestId("analytics-alert-a-1")).toBeNull();
  });

  test("renders error state when analytics endpoints fail", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes([
      {
        url: "/api/v1/self-scout/tendencies",
        body: { detail: "boom" },
        ok: false,
        status: 500,
      },
      { url: "/api/v1/alerts", body: { detail: "boom" }, ok: false, status: 500 },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
    ]);

    const { AnalyticsPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <AnalyticsPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(
        screen
          .getByTestId("analytics-card-formation-run-pass")
          .getAttribute("data-card-state"),
      ).toBe("error");
    });
  });
});
