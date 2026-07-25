import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

// Mutable navigation state shared with the hoisted next/navigation mock so each
// test can drive the active ?tab= value before rendering.
const nav = vi.hoisted(() => ({ tab: "" }));

vi.mock("next/link", () => ({
  default: ({ children, ...rest }: React.ComponentProps<"a">) => (
    <a {...rest}>{children}</a>
  ),
}));

vi.mock("next/image", () => ({
  default: () => null,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/film-room",
  useSearchParams: () => new URLSearchParams(nav.tab ? `tab=${nav.tab}` : ""),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
  nav.tab = "";
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "");
});

async function importHub() {
  vi.resetModules();
  const [{ default: FilmRoomPage }, { AppStateProvider }] = await Promise.all([
    import("./page"),
    import("@/lib/app-state"),
  ]);
  return { FilmRoomPage, AppStateProvider };
}

async function renderHub() {
  const { FilmRoomPage, AppStateProvider } = await importHub();
  render(
    <AppStateProvider>
      <FilmRoomPage />
    </AppStateProvider>,
  );
}

describe("FilmRoomPage", () => {
  test("renders the three consolidated tabs (mock clips tab is gone)", async () => {
    await renderHub();
    await waitFor(() => {
      expect(screen.getByTestId("film-room-tab-browse")).toBeTruthy();
    });
    expect(screen.getByTestId("film-room-tab-review")).toBeTruthy();
    expect(screen.getByTestId("film-room-tab-upload")).toBeTruthy();
    // The old "Clips & Highlights" tab rendered a mock clip grid — removed.
    expect(screen.queryByTestId("film-room-tab-clips")).toBeNull();
  });

  test("default tab is Browse Film (former Library)", async () => {
    await renderHub();
    // Library view is offline without NEXT_PUBLIC_API_URL.
    await waitFor(() => {
      expect(screen.getByText(/Library unavailable/i)).toBeTruthy();
    });
  });

  test("review tab renders the real per-video clip inventory", async () => {
    nav.tab = "review";
    await renderHub();
    // The heading (distinct from the tab link) proves the panel rendered.
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /Review & Tag Plays/i }),
      ).toBeTruthy();
    });
    // Offline backend, no videos → honest empty state, never a fake player.
    await waitFor(() => {
      expect(screen.getByTestId("review-empty").textContent).toMatch(
        /Backend offline|Loading film|No film yet/i,
      );
    });
  });

  test("an unknown tab falls back to Browse Film", async () => {
    nav.tab = "clips";
    await renderHub();
    await waitFor(() => {
      expect(screen.getByText(/Library unavailable/i)).toBeTruthy();
    });
  });

  test("review tab lists a video's real clips linking to /clip-review", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    const respond = (body: unknown) =>
      ({
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      }) as Response;
    const video = {
      id: "v-1",
      filename: "practice.mp4",
      status: "ready",
      created_at: "2026-07-20T10:00:00Z",
    };
    const clip = {
      id: "c-1",
      video_id: "v-1",
      start_time: 12.0,
      end_time: 18.5,
      play_number: 1,
      confidence: 0.92,
      is_reviewed: false,
      our_possession: "offense",
      session_kind: "practice",
      result_state: "preliminary",
      is_preliminary: true,
      review_state: "needs_review",
      created_at: "2026-07-20T10:00:00Z",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/v1/videos/v-1/clips")) return respond([clip]);
        if (url.includes("/api/v1/videos")) return respond([video]);
        return respond([]);
      }),
    );
    nav.tab = "review";
    await renderHub();

    const row = await screen.findByTestId("review-clip-c-1");
    expect(row.getAttribute("href")).toBe("/clip-review/?clipId=c-1");
    expect(row.textContent).toContain("Play #1");
    // Same-session result tier + review state surface as badges.
    expect(screen.getByTestId("preliminary-badge")).toBeTruthy();
    expect(screen.getByTestId("review-state-needs_review")).toBeTruthy();
  });

  test("upload tab renders the explicit Upload & Process Film view", async () => {
    nav.tab = "upload";
    await renderHub();
    await waitFor(() => {
      expect(screen.getByText(/Upload & Process Film/i)).toBeTruthy();
    });
    // Offline backend → explains what is missing rather than faking data.
    await waitFor(() => {
      expect(screen.getByTestId("processing-empty").textContent).toMatch(
        /Backend offline|Checking for uploaded film/i,
      );
    });
  });
});
