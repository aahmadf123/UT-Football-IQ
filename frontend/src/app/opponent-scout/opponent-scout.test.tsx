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
  usePathname: () => "/opponent-scout",
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

// /opponent-scout is now a thin redirect into Scouting → Opponent Prep
// (ADR 0003); the behavior under test lives in the shared OpponentScoutView.
async function importPage() {
  vi.resetModules();
  const [{ OpponentScoutView }, { AppStateProvider }] = await Promise.all([
    import("./opponent-scout-view"),
    import("@/lib/app-state"),
  ]);
  return { OpponentScoutPage: OpponentScoutView, AppStateProvider };
}

interface RouteSpec {
  url: string;
  body: unknown;
  ok?: boolean;
  status?: number;
}

function installFetchRoutes(routes: RouteSpec[]) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    calls.push(url);
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
  return { fetchMock, calls };
}

const SAMPLE_OPPONENTS = [
  {
    opponent_team: "Northern Illinois",
    video_count: 1,
    latest_recorded_at: "2026-05-20T12:00:00Z",
    videos: [
      {
        video_id: "vid-niu-1",
        filename: "niu.mp4",
        status: "ready",
        recorded_at: "2026-05-20T12:00:00Z",
        created_at: "2026-05-20T12:00:00Z",
      },
    ],
  },
];

const SAMPLE_TENDENCIES = {
  formation_tendencies: [
    {
      grouping_key: "Spread",
      total_plays: 8,
      run_count: 3,
      pass_count: 5,
      run_rate: 0.38,
      pass_rate: 0.62,
      evidence_clip_ids: [],
      low_sample: false,
    },
  ],
  motion_tendencies: {
    with_motion: { total: 0, run_count: 0, pass_count: 0, run_rate: 0, pass_rate: 0 },
    without_motion: { total: 8, run_count: 3, pass_count: 5, run_rate: 0.38, pass_rate: 0.62 },
  },
  field_zone_tendencies: [],
  personnel_tendencies: [],
  down_distance_tendencies: [],
  formation_concept_families: {},
  pre_snap_tells: [],
  alerts: [],
  clip_count: 8,
};

describe("OpponentScoutPage", () => {
  test("shows unavailable card state when NEXT_PUBLIC_API_URL is missing", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { OpponentScoutPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <OpponentScoutPage />
      </AppStateProvider>,
    );
    await waitFor(() => {
      expect(screen.getAllByTestId("card-unavailable").length).toBeGreaterThan(0);
    });
  });

  test("shows empty state when no opponent film is uploaded", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes([
      { url: "/api/v1/opponents", body: [] },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
    ]);

    const { OpponentScoutPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <OpponentScoutPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(
        screen.getAllByText(/No opponent film uploaded yet/i).length,
      ).toBeGreaterThan(0);
    });
  });

  test("loads opponents into the picker and renders tendencies when one is selected", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const { calls } = installFetchRoutes([
      { url: "/api/v1/opponents", body: SAMPLE_OPPONENTS },
      { url: "/api/v1/self-scout/opponent-tendencies", body: SAMPLE_TENDENCIES },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
    ]);

    const { OpponentScoutPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <OpponentScoutPage />
      </AppStateProvider>,
    );

    const opponentPicker = await screen.findByTestId<HTMLSelectElement>("opponent-picker");
    await waitFor(() => {
      expect(opponentPicker.options.length).toBeGreaterThan(1);
    });
    expect(
      Array.from(opponentPicker.options).some((o) => o.value === "Northern Illinois"),
    ).toBe(true);

    await act(async () => {
      fireEvent.change(opponentPicker, { target: { value: "Northern Illinois" } });
    });

    await waitFor(() => {
      expect(screen.getByText("Spread")).toBeTruthy();
    });
    expect(
      calls.find(
        (c) =>
          c.includes("/api/v1/self-scout/opponent-tendencies") &&
          c.includes("opponent_video_id=vid-niu-1"),
      ),
    ).toBeTruthy();
  });

  test("renders error state when opponents endpoint fails", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    installFetchRoutes([
      { url: "/api/v1/opponents", body: { detail: "boom" }, ok: false, status: 500 },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
    ]);

    const { OpponentScoutPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <OpponentScoutPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getAllByTestId("card-error").length).toBeGreaterThan(0);
    });
  });
});
