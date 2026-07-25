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
  usePathname: () => "/college-data",
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

// /college-data is now a thin redirect into Scouting → College Data
// (ADR 0003); the behavior under test lives in the shared CollegeDataView.
async function importPage() {
  vi.resetModules();
  const [{ CollegeDataView }, { AppStateProvider }] = await Promise.all([
    import("./college-data-view"),
    import("@/lib/app-state"),
  ]);
  return { CollegeDataPage: CollegeDataView, AppStateProvider };
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
    return { ok: true, status: 200, json: async () => ({}), text: async () => "{}" } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function cache(overrides: Record<string, unknown> = {}) {
  return {
    source: "CollegeFootballData.com",
    source_endpoint: "teams",
    sync_status: "ok",
    last_synced_at: "2026-05-28T10:00:00Z",
    stale: false,
    stale_after_hours: 168,
    row_count: 1,
    ...overrides,
  };
}

const EMPTY_TEAM = { team: null, cache: cache({ sync_status: "never", last_synced_at: null, stale: true, row_count: 0 }) };
const EMPTY_SCHEDULE = { season: null, games: [], cache: cache({ source_endpoint: "games", sync_status: "never", last_synced_at: null, stale: true, row_count: 0 }) };
const EMPTY_BENCH = { season: null, conference: "Mid-American", teams: [], cache: cache({ source_endpoint: "team_game_stats", sync_status: "never", last_synced_at: null, stale: true, row_count: 0 }) };

// NOTE: order matters — the schedule route is a superset of the team route URL.
function routes(team: unknown, schedule: unknown, bench: unknown, opts: Partial<RouteSpec> = {}): RouteSpec[] {
  return [
    { url: "/api/cfbd/teams/toledo/schedule", body: schedule, ...opts },
    { url: "/api/cfbd/teams/toledo", body: team, ...opts },
    { url: "/api/cfbd/mac/benchmark", body: bench, ...opts },
  ];
}

async function renderPage() {
  const { CollegeDataPage, AppStateProvider } = await importPage();
  render(
    <AppStateProvider>
      <CollegeDataPage />
    </AppStateProvider>,
  );
}

describe("CollegeDataPage states", () => {
  test("offline when NEXT_PUBLIC_API_URL is unset (no direct CFBD calls)", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const fetchMock = installFetchRoutes([]);
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("cfbd-banner").getAttribute("data-banner-tone")).toBe(
        "unavailable",
      );
    });
    // The page must never call CFBD (or anything) directly when offline.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("empty cache renders empty cards and no stale banner", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes(routes(EMPTY_TEAM, EMPTY_SCHEDULE, EMPTY_BENCH));
    await renderPage();
    await waitFor(() => {
      expect(
        screen.getByTestId("analytics-card-toledo-team").getAttribute("data-card-state"),
      ).toBe("empty");
    });
    expect(
      screen.getByTestId("analytics-card-toledo-schedule").getAttribute("data-card-state"),
    ).toBe("empty");
    // Never-synced empty cache should NOT show a misleading stale banner.
    expect(screen.queryByTestId("cfbd-banner")).toBeNull();
    expect(screen.getByTestId("cfbd-source-label").textContent).toContain(
      "CollegeFootballData.com",
    );
  });

  test("renders live cached Toledo data and MAC benchmark", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const team = {
      team: {
        cfbd_team_id: 2649,
        school: "Toledo",
        mascot: "Rockets",
        abbreviation: "TOL",
        conference: "Mid-American",
        division: "West",
        color: "#15397F",
        alt_color: "#FFD20A",
      },
      cache: cache(),
    };
    const schedule = {
      season: 2025,
      games: [
        {
          cfbd_game_id: 401,
          season: 2025,
          week: 1,
          season_type: "regular",
          start_date: "2025-08-30T16:00:00Z",
          home_team: "Toledo",
          away_team: "Akron",
          home_points: 31,
          away_points: 10,
          venue: "Glass Bowl",
          completed: true,
        },
      ],
      cache: cache({ source_endpoint: "games" }),
    };
    const bench = {
      season: 2025,
      conference: "Mid-American",
      teams: [
        {
          team: "Toledo",
          conference: "Mid-American",
          games: 2,
          avg_points_for: 27.5,
          avg_points_against: 15.0,
          point_differential: 12.5,
        },
      ],
      cache: cache({ source_endpoint: "team_game_stats" }),
    };
    installFetchRoutes(routes(team, schedule, bench));
    await renderPage();
    await waitFor(() => {
      expect(screen.getByText("Rockets")).toBeTruthy();
    });
    expect(screen.getByTestId("cfbd-schedule").textContent).toContain("Akron @ Toledo");
    expect(screen.getByTestId("cfbd-benchmark").textContent).toContain("Toledo");
    // Fresh data -> no banner.
    expect(screen.queryByTestId("cfbd-banner")).toBeNull();
  });

  test("stale cache shows a stale warning banner", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const staleCache = cache({ stale: true, last_synced_at: "2026-01-01T00:00:00Z" });
    const team = { team: { cfbd_team_id: 1, school: "Toledo", mascot: "Rockets", abbreviation: "TOL", conference: "Mid-American", division: "West", color: null, alt_color: null }, cache: staleCache };
    installFetchRoutes(routes(team, { season: null, games: [], cache: staleCache }, { season: null, conference: "Mid-American", teams: [], cache: staleCache }));
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("cfbd-banner").getAttribute("data-banner-tone")).toBe("stale");
    });
  });

  test("CFBD unavailable (sync error) shows unavailable banner", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const errCache = cache({ sync_status: "error", last_synced_at: null, stale: true, row_count: 0 });
    installFetchRoutes(
      routes(
        { team: null, cache: errCache },
        { season: null, games: [], cache: errCache },
        { season: null, conference: "Mid-American", teams: [], cache: errCache },
      ),
    );
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("cfbd-banner").getAttribute("data-banner-tone")).toBe(
        "unavailable",
      );
    });
  });

  test("backend error shows error card and unavailable banner", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes(
      routes({ detail: "boom" }, { detail: "boom" }, { detail: "boom" }, { ok: false, status: 500 }),
    );
    await renderPage();
    await waitFor(() => {
      expect(
        screen.getByTestId("analytics-card-toledo-team").getAttribute("data-card-state"),
      ).toBe("error");
    });
    expect(screen.getByTestId("cfbd-banner").getAttribute("data-banner-tone")).toBe(
      "unavailable",
    );
  });
});
