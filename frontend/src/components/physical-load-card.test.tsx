import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

const fetchPhysical = vi.fn();
vi.mock("@/lib/api", () => ({
  fetchPlayerPhysical: (...args: unknown[]) => fetchPhysical(...args),
}));

import { PhysicalLoadCard } from "./physical-load-card";

const load = {
  player_id: "p1",
  reps: 12,
  seconds: 80,
  yards: 412.4,
  high_speed_yards: 61.2,
  sprint_yards: 18.0,
  sprints: 3,
  high_speed_efforts: 7,
  accelerations: 9,
  decelerations: 6,
  top_speed_yps: 9.1,
  peak_accel_yps2: 4.4,
  peak_decel_yps2: 5.2,
  get_off_yards: 2.35,
  get_off_reps: 10,
  band_yards: { walk: 90, jog: 140, run: 121.2, high_speed: 43.2, sprint: 18 },
};

afterEach(() => {
  cleanup();
  fetchPhysical.mockReset();
});

describe("PhysicalLoadCard", () => {
  test("shows top speed in mph, efforts, get-off and the speed bands", async () => {
    fetchPhysical.mockResolvedValue({
      load,
      sessions: [
        { day: "2026-10-01", reps: 6, yards: 200, high_speed_yards: 30, sprints: 1, top_speed_yps: 8.8 },
        { day: "2026-10-08", reps: 6, yards: 212.4, high_speed_yards: 31.2, sprints: 2, top_speed_yps: 9.1 },
      ],
    });
    render(<PhysicalLoadCard playerId="p1" token="t" />);
    expect(await screen.findByText("18.6")).toBeTruthy(); // 9.1 yd/s
    expect(screen.getByText("412")).toBeTruthy();
    expect(screen.getByText("2.4")).toBeTruthy();
    expect(screen.getByText(/10 REPS/)).toBeTruthy();
    expect(screen.getByRole("img", { name: /Sprint 18 yards/ })).toBeTruthy();
  });

  test("explains when a player has no measured reps", async () => {
    fetchPhysical.mockResolvedValue({ load: null, sessions: [] });
    render(<PhysicalLoadCard playerId="p1" />);
    expect(await screen.findByText(/No measured reps yet/)).toBeTruthy();
  });

  test("shows a plain-language error", async () => {
    fetchPhysical.mockRejectedValue(new TypeError("Failed to fetch"));
    render(<PhysicalLoadCard playerId="p1" />);
    expect((await screen.findByRole("alert")).textContent).toContain("physical load");
  });
});
