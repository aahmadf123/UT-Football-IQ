import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

const fetchLoads = vi.fn();
vi.mock("@/lib/api", () => ({
  fetchPhysicalLoads: (...args: unknown[]) => fetchLoads(...args),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("@/lib/app-state", () => ({
  YDS_PER_SEC_TO_MPH: 2.0455,
  useAppState: () => ({
    authToken: "t",
    data: {
      players: [
        { id: "wr", jersey: 1, name: "Fast Receiver", position: "WR", group: "WR" },
        { id: "lb", jersey: 44, name: "Thumper", position: "LB", group: "LB" },
        { id: "db", jersey: 2, name: "Corner", position: "CB", group: "DB" },
      ],
    },
  }),
}));

import { PhysicalLeaderboard } from "./physical-leaderboard";

function load(id: string, top: number, sprints: number, getOff: number | null) {
  return {
    player_id: id,
    reps: 4,
    seconds: 30,
    yards: 120,
    high_speed_yards: 20,
    sprint_yards: 5,
    sprints,
    high_speed_efforts: 3,
    accelerations: 2,
    decelerations: 2,
    top_speed_yps: top,
    peak_accel_yps2: 4,
    peak_decel_yps2: 4,
    get_off_yards: getOff,
    get_off_reps: getOff == null ? 0 : 4,
    band_yards: {},
  };
}

afterEach(() => {
  cleanup();
  fetchLoads.mockReset();
});

function names(): string[] {
  return within(screen.getByRole("table"))
    .getAllByRole("link")
    .map((a) => a.textContent ?? "");
}

describe("PhysicalLeaderboard", () => {
  test("ranks by the chosen metric and filters by position group", async () => {
    fetchLoads.mockResolvedValue([load("lb", 8.0, 1, 2.1), load("wr", 9.5, 4, null), load("db", 9.0, 2, 2.6)]);
    render(<PhysicalLeaderboard />);
    expect(await screen.findByText("19.4")).toBeTruthy(); // 9.5 yd/s in mph
    expect(names()).toEqual(["#1 Fast Receiver", "#2 Corner", "#44 Thumper"]);

    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "get_off" } });
    // No snap seen for the receiver: left off rather than ranked as zero.
    expect(names()).toEqual(["#2 Corner", "#44 Thumper"]);

    fireEvent.change(screen.getByLabelText("Position group"), { target: { value: "LB" } });
    expect(names()).toEqual(["#44 Thumper"]);
  });

  test("explains an empty leaderboard", async () => {
    fetchLoads.mockResolvedValue([]);
    render(<PhysicalLeaderboard />);
    expect(await screen.findByText("No measured reps yet")).toBeTruthy();
  });
});
