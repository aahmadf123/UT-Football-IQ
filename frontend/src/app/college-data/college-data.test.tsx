import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
  method?: string;
}

function installFetchRoutes(routes: RouteSpec[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const r of routes) {
      if (url.includes(r.url) && (!r.method || r.method === (init?.method ?? "GET"))) {
        const ok = r.ok ?? true;
        return {
          ok,
          status: r.status ?? (ok ? 200 : 500),
          json: async () => r.body,
          text: async () => JSON.stringify(r.body),
        } as unknown as Response;
      }
    }
    return { ok: true, status: 200, json: async () => [], text: async () => "[]" } as unknown as Response;
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
  test("a slow previous season cannot overwrite the selected season", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const originalFetch = installFetchRoutes(routes(EMPTY_TEAM, EMPTY_SCHEDULE, EMPTY_BENCH));
    let resolveOld!: (value: Response) => void;
    const oldSeasonResponse = new Promise<Response>((resolve) => { resolveOld = resolve; });
    const currentYear = new Date().getFullYear();
    const makeSchedule = (opponent: string) => ({ season: currentYear, cache: cache(), games: [{ cfbd_game_id: 401, season: currentYear, home_team: "Toledo", away_team: opponent }] });
    const response = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes(`/schedule?season=${currentYear}`)) return oldSeasonResponse;
      if (url.includes(`/schedule?season=${currentYear - 1}`)) return Promise.resolve(response(makeSchedule("Current selection")));
      return originalFetch(input, init);
    });
    vi.stubGlobal("fetch", fetchMock);
    await renderPage();
    await waitFor(() => expect(fetchMock.mock.calls.some((call) => String(call[0]).includes(`/schedule?season=${currentYear}`))).toBe(true));
    fireEvent.change(screen.getByLabelText("Season"), { target: { value: String(currentYear - 1) } });
    await screen.findByRole("button", { name: "Current selection @ Toledo" });
    await act(async () => { resolveOld(response(makeSchedule("Stale season"))); await oldSeasonResponse; });
    expect(screen.getByRole("button", { name: "Current selection @ Toledo" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stale season @ Toledo" })).toBeNull();
  });

  test("a schedule entry opens its cached play-by-play", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes([
      { url: "/api/cfbd/games/401/plays", body: { game_id: 401, cache: cache(), plays: [{ cfbd_play_id: 9, offense: "Toledo", defense: "Akron", period: 1, down: 3, distance: 4, yard_line: 30, yards_gained: 6, play_type: "Rush", play_text: "Recorded third-down run" }] } },
      ...routes(EMPTY_TEAM, { season: 2026, cache: cache(), games: [{ cfbd_game_id: 401, season: 2026, home_team: "Toledo", away_team: "Akron" }] }, EMPTY_BENCH),
    ]);
    await renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Akron @ Toledo" }));
    expect(await screen.findByText("Recorded third-down run")).toBeTruthy();
    expect(screen.getByText("3 & 4")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close game plays" }));
    expect(screen.queryByText("Recorded third-down run")).toBeNull();
  });

  test("administrators refresh bounded scopes through the team API", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "admin");
    const fetchMock = installFetchRoutes([
      { url: "/api/cfbd/status", body: { configured: true, source: "CollegeFootballData.com" } },
      { url: "/api/cfbd/sync", body: { status: "succeeded", rows_written: 4, message: "Updated" } },
      ...routes(EMPTY_TEAM, EMPTY_SCHEDULE, EMPTY_BENCH),
    ]);
    await renderPage();
    const button = await screen.findByRole("button", { name: "Refresh season data" });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    await screen.findByText(/Updated 12 cached records/);
    const writes = fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/sync"));
    expect(writes).toHaveLength(3);
    const bodies = writes.map((call) => JSON.parse(String((call[1] as RequestInit).body)));
    expect(bodies.map((body) => body.scope)).toEqual(["teams", "schedule", "benchmark"]);
    expect(bodies.every((body) => Object.keys(body).sort().join(",") === "scope,season,season_type")).toBe(true);
    expect(fetchMock.mock.calls.every((call) => String(call[0]).startsWith("https://api.test/"))).toBe(true);
  });

  test("missing server connection disables quota-consuming refresh", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "admin");
    installFetchRoutes([
      { url: "/api/cfbd/status", body: { configured: false, source: "CollegeFootballData.com" } },
      ...routes(EMPTY_TEAM, EMPTY_SCHEDULE, EMPTY_BENCH),
    ]);
    await renderPage();
    await screen.findByText(/CFBD key is not configured/);
    expect((screen.getByRole("button", { name: "Refresh season data" }) as HTMLButtonElement).disabled).toBe(true);
  });

  test("uses the local API and surfaces unavailable cached data without calling CFBD directly", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("cfbd-banner").getAttribute("data-banner-tone")).toBe(
        "unavailable",
      );
    });
    expect(fetchMock).toHaveBeenCalled();
    expect(fetchMock.mock.calls.every((call) => String(call[0]).startsWith("/api/"))).toBe(true);
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
