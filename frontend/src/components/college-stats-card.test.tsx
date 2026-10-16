import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

const fetchSeasons = vi.fn();
vi.mock("@/lib/cfbd-admin", () => ({
  fetchPlayerCollegeSeasons: (...args: unknown[]) => fetchSeasons(...args),
}));

import { CollegeStatsCard } from "./college-stats-card";

afterEach(() => {
  cleanup();
  fetchSeasons.mockReset();
});

describe("CollegeStatsCard", () => {
  test("shows usage, PPA and box-score categories for the latest season", async () => {
    fetchSeasons.mockResolvedValue({
      player_id: "p1",
      cfbd_athlete_id: "101",
      seasons: [
        {
          season: 2025,
          team: "Toledo",
          position: "RB",
          stats: { rushing: { YDS: "512", CAR: "88" } },
          usage: { overall: 0.044, thirdDown: 0.1 },
          ppa: { average: { all: 0.403 }, total: { all: 35.5 } },
        },
      ],
    });
    render(<CollegeStatsCard playerId="p1" token="t" />);
    expect(await screen.findByText("Rushing")).toBeTruthy();
    expect(screen.getByText("512")).toBeTruthy();
    expect(screen.getByText("4.4%")).toBeTruthy();
    expect(screen.getByText("0.40")).toBeTruthy();
  });

  test("explains when a player has no official stats yet", async () => {
    fetchSeasons.mockResolvedValue({ player_id: "p1", cfbd_athlete_id: null, seasons: [] });
    render(<CollegeStatsCard playerId="p1" />);
    expect(await screen.findByText(/No official game stats yet/)).toBeTruthy();
  });

  test("shows a plain-language error", async () => {
    fetchSeasons.mockRejectedValue(new TypeError("Failed to fetch"));
    render(<CollegeStatsCard playerId="p1" />);
    expect((await screen.findByRole("alert")).textContent).toContain("Couldn't load college stats");
  });
});
