import type { ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ApiPlayer, PlayerSummary } from "@/lib/types";

const model = vi.hoisted(() => ({ params: "", mockMode: true, players: [] as PlayerSummary[], push: vi.fn(), back: vi.fn(), select: vi.fn(), fetch: vi.fn(), metrics: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: model.push, back: model.back }), useSearchParams: () => new URLSearchParams(model.params) }));
vi.mock("@/lib/api", () => ({ fetchPlayer: model.fetch, fetchPlayerMetricsDetail: model.metrics }));
vi.mock("@/lib/app-state", () => ({
  useAppState: () => ({ data: { players: model.players }, getPlayerById: (id: string) => model.players.find((row) => row.id === id), setSelectedPlayerId: model.select, mockMode: model.mockMode, authToken: "staff" }),
  apiPlayerToSummary: (row: ApiPlayer): PlayerSummary => ({ id: row.id, jersey: String(row.jersey_number), name: `${row.first_name} ${row.last_name}`, position: row.position ?? "", group: row.position_group ?? "" }),
  mergePlayerMetrics: (rows: PlayerSummary[]) => rows,
}));
vi.mock("@/components/shell/app-shell", () => ({ FootballShell: ({ children }: { children: ReactNode }) => <main>{children}</main> }));
vi.mock("@/components/shared/player-portrait", () => ({ PlayerPortrait: () => null }));
vi.mock("@/components/shared/trend-line", () => ({ TrendLine: () => null }));
vi.mock("@/components/players-view", () => ({ fmtMetric: (value: number | undefined) => value ?? "—" }));
vi.mock("@/components/physical-load-card", () => ({ PhysicalLoadCard: () => null }));
vi.mock("@/components/college-stats-card", () => ({ CollegeStatsCard: () => null }));
import { PlayerProfileClient } from "./player-profile-client";

const alice: PlayerSummary = { id: "alice", jersey: "12", name: "Jordan Local", position: "QB", group: "QB" };
const bob: PlayerSummary = { ...alice, id: "bob", name: "Casey Peer", jersey: "15" };
beforeEach(() => { model.mockMode = true; model.params = ""; model.players = [alice, bob]; });
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllEnvs(); });

test("local profiles fetch actual identity with no build-time remote API origin", async () => {
  vi.stubEnv("NEXT_PUBLIC_API_URL", "");
  model.mockMode = false; model.players = [];
  model.fetch.mockResolvedValue({ id: "alice", first_name: "Jordan", last_name: "Local", jersey_number: 12, position: "QB", position_group: "QB", metadata: { height_in: 75 } });
  model.metrics.mockResolvedValue({ summary: { tracklet_count: 0 } });
  render(<PlayerProfileClient id="alice" />);
  await waitFor(() => expect(model.fetch).toHaveBeenCalledWith("alice", "staff"));
  expect(await screen.findByText("Jordan Local")).toBeTruthy();
  expect(screen.getByText("6'3\"")).toBeTruthy();
  expect(screen.queryByText(/Not connected to the team server/)).toBeNull();
});

test("player switching and peer links preserve cohort and deterministic safe Back", () => {
  model.params = "id=alice&position=QB&session_ids=practice&drill_type=7on7&from=%2Fteam-stats%2F%3Fformation%3DTrips";
  render(<PlayerProfileClient id="alice" />);
  const back = new URL(screen.getByRole("link", { name: "Back" }).getAttribute("href")!, "https://film.test");
  expect(back.pathname.replace(/\/$/, "")).toBe("/team-stats");
  expect(back.searchParams.get("formation")).toBe("Trips");
  const peer = screen.getByRole("link", { name: /Casey Peer/ }).getAttribute("href")!;
  const peerParams = new URL(peer, "https://film.test").searchParams;
  expect(peerParams.get("session_ids")).toBe("practice");
  expect(peerParams.get("drill_type")).toBe("7on7");
  expect(peerParams.get("player_id")).toBe("bob");
  expect(peerParams.get("from")).toBe("/team-stats/?formation=Trips");
  fireEvent.change(screen.getByLabelText("Switch player"), { target: { value: "bob" } });
  const pushed = new URL(model.push.mock.calls[0][0], "https://film.test");
  expect(pushed.searchParams.toString()).toBe(peerParams.toString());
  expect(model.back).not.toHaveBeenCalled();
});

test("malformed external return context uses roster and the analysis link uses the actual player", () => {
  model.params = "from=%2F%2Fevil.test&session_ids=practice&contact_level=thud&player_id=stale";
  render(<PlayerProfileClient id="alice" />);
  expect(new URL(screen.getByRole("link", { name: "Back" }).getAttribute("href")!, "https://film.test").pathname.replace(/\/$/, "")).toBe("/players");
  const target = new URL(screen.getByRole("link", { name: "Analyze filtered film" }).getAttribute("href")!, "https://film.test");
  expect(target.pathname.replace(/\/$/, "")).toBe("/team-stats");
  expect(target.searchParams.get("player_id")).toBe("alice");
  expect(target.searchParams.get("contact_level")).toBe("thud");
  expect(target.searchParams.get("session_ids")).toBe("practice");
  expect(screen.getByText(/All qualified tracked film for this player/)).toBeTruthy();
});
