import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

const fetchVideos = vi.fn();
const fetchGameSummary = vi.fn();
vi.mock("@/lib/api", () => ({
  fetchVideos: (...args: unknown[]) => fetchVideos(...args),
  fetchGameSummary: (...args: unknown[]) => fetchGameSummary(...args),
}));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "t" }) }));
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

import { GameSummaryView, playlistHref } from "./game-summary-view";

const side = {
  plays: 62,
  yards: 389,
  yards_per_play: 6.27,
  success_rate: 0.452,
  epa_per_play: 0.118,
  rush_plays: 30,
  rush_yards: 150,
  rush_yards_per_play: 5.0,
  rush_success_rate: 0.4,
  pass_plays: 32,
  pass_yards: 239,
  pass_yards_per_play: 7.47,
  pass_success_rate: 0.5,
  third_down_attempts: 12,
  third_down_conversions: 5,
  explosive_plays: 7,
  turnovers: 1,
  by_down: [{ down: 3, plays: 12, success_rate: 0.417, yards_per_play: 5.1, rush_share: 0.25, clip_ids: ["c3", "c4"] }],
  groups: [
    { key: "third_down_converted", label: "3rd-down conversions", plays: 2, clip_ids: ["c1", "c2"] },
  ],
};

afterEach(() => {
  cleanup();
  fetchVideos.mockReset();
  fetchGameSummary.mockReset();
});

describe("GameSummaryView", () => {
  test("shows the newest game's offense with links to the plays behind each number", async () => {
    fetchVideos.mockResolvedValue([
      { id: "old", filename: "a.mp4", recorded_at: "2026-08-29T00:00:00Z", opponent_team: "UMass", created_at: "" },
      { id: "new", filename: "b.mp4", recorded_at: "2026-09-05T00:00:00Z", opponent_team: "Michigan State", created_at: "" },
    ]);
    fetchGameSummary.mockResolvedValue({
      video_id: "new",
      opponent: "Michigan State",
      film_plays: 161,
      linked_plays: 115,
      offense: side,
      defense: null,
    });
    render(<GameSummaryView />);
    expect(await screen.findByText("5/12")).toBeTruthy();
    expect(fetchGameSummary).toHaveBeenCalledWith("new", "t");
    expect(screen.getByText(/115 of 161 film plays/)).toBeTruthy();
    const link = screen.getByRole("link", { name: /3rd-down conversions/ });
    expect(link.getAttribute("href")).toBe(playlistHref(["c1", "c2"]));
    expect(screen.getByRole("link", { name: "3rd down plays" }).getAttribute("href")).toBe(
      playlistHref(["c3", "c4"]),
    );

    fireEvent.click(screen.getByRole("button", { name: "Toledo defense" }));
    expect(screen.getByText("No Toledo defense plays matched in this film.")).toBeTruthy();
  });

  test("loads subsequent pages before selecting the newest game", async () => {
    const firstPage = Array.from({ length: 200 }, (_, index) => ({
      id: `game-${index}`,
      filename: `game-${index}.mp4`,
      recorded_at: `2026-09-${String(5 - (index % 5)).padStart(2, "0")}T00:00:00Z`,
      opponent_team: "Opponent",
      created_at: "",
    }));
    fetchVideos.mockResolvedValueOnce(firstPage).mockResolvedValueOnce([
      {
        id: "older",
        filename: "older.mp4",
        recorded_at: "2020-09-05T00:00:00Z",
        opponent_team: "Opponent",
        created_at: "",
      },
    ]);
    fetchGameSummary.mockResolvedValue({
      video_id: "game-0",
      opponent: "Opponent",
      film_plays: 0,
      linked_plays: 0,
      offense: null,
      defense: null,
    });

    render(<GameSummaryView />);

    expect(await screen.findByText(/hasn't been split into plays yet/)).toBeTruthy();
    expect(fetchVideos).toHaveBeenNthCalledWith(
      1,
      { session_kind: "game", limit: 200, offset: 0 },
      "t",
    );
    expect(fetchVideos).toHaveBeenNthCalledWith(
      2,
      { session_kind: "game", limit: 200, offset: 200 },
      "t",
    );
    expect(fetchGameSummary).toHaveBeenCalledWith("game-0", "t");
  });

  test("explains a game whose plays aren't matched yet", async () => {
    fetchVideos.mockResolvedValue([
      { id: "g", filename: "g.mp4", recorded_at: null, opponent_team: null, created_at: "" },
    ]);
    fetchGameSummary.mockResolvedValue({
      video_id: "g",
      opponent: null,
      film_plays: 161,
      linked_plays: 0,
      offense: null,
      defense: null,
    });
    render(<GameSummaryView />);
    expect(await screen.findByText(/161 plays found/)).toBeTruthy();
  });

  test("invites an upload when there is no game film", async () => {
    fetchVideos.mockResolvedValue([]);
    render(<GameSummaryView />);
    expect(await screen.findByText("No game film yet")).toBeTruthy();
  });

  test("charts every snap from the play log and opens the plays behind them", async () => {
    const play = (n: number, yards: number, extra: Record<string, unknown> = {}) => ({
      clip_id: `p${n}`,
      play_number: n,
      period: n <= 2 ? 1 : 2,
      down: 1,
      distance: 10,
      yards_gained: yards,
      kind: "rush",
      epa: yards / 10,
      success: yards >= 5,
      explosive: yards >= 10,
      turnover: false,
      scoring: false,
      text: `Run for ${yards}`,
      ...extra,
    });
    const log = [play(1, 12), play(2, -3), play(3, 4, { scoring: true }), play(4, 25, { kind: "pass" })];
    fetchVideos.mockResolvedValue([{ id: "g", filename: "g.mp4", recorded_at: "2026-09-05T00:00:00Z", opponent_team: "Michigan State", created_at: "" }]);
    fetchGameSummary.mockResolvedValue({ video_id: "g", opponent: "Michigan State", film_plays: 4, linked_plays: 4, offense: { ...side, play_log: log }, defense: null });
    render(<GameSummaryView />);
    expect(await screen.findByRole("img", { name: "Yards gained per play" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Cumulative EPA through the game" })).toBeTruthy();
    expect(screen.getByRole("table", { name: "Yards per play by quarter" })).toBeTruthy();
    // Every snap is its own target, labelled with what happened.
    expect(screen.getByRole("button", { name: /\+25 yd pass, Q2/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /-3 yd rush, Q1 · 1st & 10 · not successful/ })).toBeTruthy();
    // Yards buckets: one loss, one 4-6, one 10-19, one 20+.
    expect(screen.getByRole("button", { name: /^1 plays, Loss yards/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^0 plays, 7–9 yards/ })).toBeTruthy();
    // Clicking a snap watches the game from that play on.
    fireEvent.click(screen.getByRole("button", { name: /\+25 yd pass, Q2/ }));
    expect(push).toHaveBeenCalledWith(playlistHref(["p4", "p1", "p2", "p3"]));
  });
});
