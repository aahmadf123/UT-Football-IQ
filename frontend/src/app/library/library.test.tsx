import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
  test("attempts the local API and explains a connection failure", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    const { LibraryPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <LibraryPage />
      </AppStateProvider>,
    );
    await waitFor(() => {
      expect(screen.getByText(/Could not load library/i)).toBeTruthy();
    });
    expect(fetchMock).toHaveBeenCalled();
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
        practice_session_id: "s-practice-1",
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
        practice_session_id: "s-practice-1",
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
        json: async () => url.includes("/api/v1/videos") ? videos : [],
        text: async () => JSON.stringify(url.includes("/api/v1/videos") ? videos : []),
      };
    });
    vi.stubGlobal("fetch", fetchMock);

    const { LibraryPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <LibraryPage savedSessions={[{ id: "s-practice-1", title: "Wednesday team period" }]} />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText(/Practice & Scrimmage Sessions/i)).toBeTruthy();
      // "Game Film" appears in two places (section header + sidebar nav) so
      // verify by opponent presence instead.
      expect(screen.getAllByText(/Northern Illinois/i).length).toBeGreaterThan(0);
      expect(screen.getByRole("button", { name: /Expand session Wednesday team period.*May 20, 2026/ })).toBeTruthy();
      expect(screen.getByText(/May 20, 2026 · Practice/)).toBeTruthy();
    });
  });

  test("keeps ungrouped uploads visible and searches camera labels with a recoverable empty state", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const videos = [{ id: "v-ungrouped", filename: "team-period.mp4", camera_label: "End zone", status: "uploaded", created_at: "2026-05-20T16:00:00Z" }];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const body = String(input).includes("/api/v1/videos") ? videos : [];
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    }));
    const { LibraryPage, AppStateProvider } = await importPage();
    render(<AppStateProvider><LibraryPage /></AppStateProvider>);
    await screen.findByRole("button", { name: /Expand session.*May 20, 2026/ });
    fireEvent.change(screen.getByLabelText("Search film"), { target: { value: "end zone" } });
    await waitFor(() => expect(screen.queryByText("No film matches these filters")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: /Expand session.*May 20, 2026/ }));
    const watchLink = await screen.findByRole("link", { name: "Watch recording" });
    expect(watchLink.getAttribute("href")).toBe("/clip-review/?videoId=v-ungrouped");
    fireEvent.change(screen.getByLabelText("Search film"), { target: { value: "missing recording" } });
    await screen.findByText("No film matches these filters");
    fireEvent.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    await screen.findByRole("button", { name: /Expand session.*May 20, 2026/ });
    expect((screen.getByLabelText("Search film") as HTMLInputElement).value).toBe("");
  });

  test("loads the next recording page instead of hiding film after the first 200 uploads", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const videos = Array.from({ length: 200 }, (_, index) => ({ id: `v-${index}`, filename: `recording-${index}.mp4`, status: "uploaded", created_at: "2026-05-20T16:00:00Z" }));
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes("/api/v1/videos")
        ? url.includes("offset=200") ? [{ ...videos[0], id: "v-201", filename: "last-recording.mp4" }] : videos
        : [];
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const { LibraryPage, AppStateProvider } = await importPage();
    render(<AppStateProvider><LibraryPage /></AppStateProvider>);
    // 200 rows take well over the default 1 s to render on CI runners.
    const slow = { timeout: 10_000 };
    fireEvent.click(await screen.findByRole("button", { name: "Load more recordings" }, slow));
    await screen.findByText("201 loaded", {}, slow);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("offset=200"))).toBe(true);
    expect(screen.queryByRole("button", { name: "Load more recordings" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Search film"), { target: { value: "last-recording" } });
    fireEvent.click(await screen.findByRole("button", { name: /Expand session.*May 20, 2026/ }, slow));
    await screen.findByRole("link", { name: "last-recording.mp4" }, slow);
  }, 30_000);
});
