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
  usePathname: () => "/library",
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

// /library is now a thin redirect into Film Room → Browse Film (ADR 0003);
// the behavior under test lives in the shared LibraryView the hub renders.
async function importPage() {
  vi.resetModules();
  const [{ LibraryView }, { AppStateProvider }] = await Promise.all([
    import("./library-view"),
    import("@/lib/app-state"),
  ]);
  return { LibraryPage: LibraryView, AppStateProvider };
}

describe("LibraryPage", () => {
  test("renders an offline notice when NEXT_PUBLIC_API_URL is missing", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { LibraryPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <LibraryPage />
      </AppStateProvider>,
    );
    await waitFor(() => {
      expect(screen.getByText(/Library unavailable/i)).toBeTruthy();
    });
  });

  test("renders empty state when backend returns no sessions and no videos", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    }));
    vi.stubGlobal("fetch", fetchMock);

    const { LibraryPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <LibraryPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText(/No film yet/i)).toBeTruthy();
    });
  });

  test("renders practice and game sections from backend data", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");

    const sessions = [
      {
        practice_session_id: null,
        session_date: "2026-05-20",
        session_kind: "practice",
        opponent_team: null,
        video_count: 1,
        first_recorded_at: "2026-05-20T15:00:00Z",
        last_recorded_at: "2026-05-20T17:00:00Z",
      },
      {
        practice_session_id: null,
        session_date: "2026-05-18",
        session_kind: "game",
        opponent_team: "Northern Illinois",
        video_count: 1,
        first_recorded_at: "2026-05-18T19:00:00Z",
        last_recorded_at: "2026-05-18T22:00:00Z",
      },
    ];
    const videos = [
      {
        id: "v-practice-1",
        filename: "practice-may-20.mp4",
        status: "ready",
        created_at: "2026-05-20T16:00:00Z",
        recorded_at: "2026-05-20T16:00:00Z",
        session_kind: "practice",
        opponent_team: null,
        our_possession: null,
      },
      {
        id: "v-game-1",
        filename: "game-may-18.mp4",
        status: "ready",
        created_at: "2026-05-18T20:00:00Z",
        recorded_at: "2026-05-18T20:00:00Z",
        session_kind: "game",
        opponent_team: "Northern Illinois",
        our_possession: "offense",
      },
    ];

    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/practice-sessions")) {
        return {
          ok: true,
          status: 200,
          json: async () => sessions,
          text: async () => JSON.stringify(sessions),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => videos,
        text: async () => JSON.stringify(videos),
      };
    });
    vi.stubGlobal("fetch", fetchMock);

    const { LibraryPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <LibraryPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText(/Practice & Scrimmage Sessions/i)).toBeTruthy();
      // "Game Film" appears in two places (section header + sidebar nav) so
      // verify by opponent presence instead.
      expect(screen.getAllByText(/Northern Illinois/i).length).toBeGreaterThan(0);
    });
  });
});
