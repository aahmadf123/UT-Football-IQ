import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { TeamAnalytics } from "./team-analytics";
import * as api from "@/lib/analytics-api";

const navigation = vi.hoisted(() => ({ replace: vi.fn(), query: "position=WR&session_kind=practice&layout=analyst" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: navigation.replace }), useSearchParams: () => new URLSearchParams(navigation.query), usePathname: () => "/team-stats/" }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "staff", selectedDate: "", sessionType: "all", data: { players: [] } }) }));
vi.mock("@/components/charts/echart", () => ({ EChart: ({ label, onSelect }: { label: string; onSelect: (index: number) => void }) => <button onClick={() => onSelect(0)}>{label}</button> }));
vi.mock("@/components/game-summary-view", () => ({ GameSummaryView: () => <p>Official game outcomes</p> }));
vi.mock("@/lib/analytics-api", () => ({ fetchMetricDefinitions: vi.fn(), queryAnalytics: vi.fn(), fetchSavedViews: vi.fn(), saveAnalysisView: vi.fn(), deleteAnalysisView: vi.fn(), fetchPlayEvidence: vi.fn() }));

const row = { play_id: "play-a", clip_id: "clip-a", metric_id: "metric-a", value: 8, video_id: "video", start_seconds: 10, end_seconds: 20 };
const result: api.AnalyticsResult = { dataset_version: "measured-v1", cohort: { plays: 2, player_reps: 2, eligible_measurements: 2, excluded_measurements: 1, unknown_count: 1 }, groups: [{ key: "all", metric_name: "route_depth", n: 2, mean: 8, median: 8, p10: 6.4, p90: 9.6, min: 6, max: 10, unit: "yd", low_sample: true, evidence: [row] }], exclusions: [{ reason: "calibration_missing", count: 1 }] };
beforeEach(() => {
  navigation.query = "position=WR&session_kind=practice&layout=analyst";
  vi.mocked(api.fetchMetricDefinitions).mockResolvedValue({ definitions: [{ name: "route_depth", label: "Route depth", definition: "Forward distance from LOS", unit: "yd", version: "1", value_kind: "observed", availability: "qualified" }] });
  vi.mocked(api.queryAnalytics).mockResolvedValue(result);
  vi.mocked(api.fetchSavedViews).mockResolvedValue([]);
  vi.mocked(api.fetchPlayEvidence).mockResolvedValue({ play_id: "play-a", views: [{ clip_id: "clip-a", video_id: "video", start_seconds: 10, end_seconds: 20, camera_label: "Drone" }], metrics: [] });
  vi.mocked(api.saveAnalysisView).mockResolvedValue({ id: "view-1", name: "Receivers", surface: "analytics", visibility: "private", filters: { position: "WR" } });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

test("cohort denominators, definitions and exclusions accompany observed distributions", async () => {
  render(<TeamAnalytics />);
  await screen.findByRole("heading", { name: "Route depth" });
  expect(screen.getByTestId("cohort-denominator").textContent).toContain("2 canonical plays");
  expect(screen.getByTestId("cohort-denominator").textContent).toContain("1 unknown");
  expect(screen.getByText(/Definition 1/).textContent).toContain("Forward distance from LOS");
  expect(screen.getByText("1 · calibration missing")).toBeTruthy();
  expect(api.queryAnalytics).toHaveBeenCalledWith(expect.objectContaining({ position: "WR", session_kind: "practice" }), "staff");
});
test("chart selection loads evidence and film drilldown preserves cohort and Back", async () => {
  render(<TeamAnalytics />);
  fireEvent.click(await screen.findByRole("button", { name: /Route depth, 2 eligible/ }));
  await screen.findByText(/Canonical play play-a/);
  expect(api.fetchPlayEvidence).toHaveBeenCalledWith("play-a", "staff");
  const link = screen.getByRole("link", { name: "Watch play" });
  const url = new URL(link.getAttribute("href")!, "https://film.test");
  expect(url.searchParams.get("position")).toBe("WR");
  expect(url.searchParams.get("from")).toContain("/team-stats/");
});
test("saved views persist current typed context and selected visibility", async () => {
  render(<TeamAnalytics />);
  await screen.findByRole("heading", { name: "Route depth" });
  fireEvent.click(screen.getByText("Saved views"));
  fireEvent.change(screen.getByLabelText("View name"), { target: { value: "Receivers" } });
  fireEvent.click(screen.getByRole("button", { name: "Save current view" }));
  await waitFor(() => expect(api.saveAnalysisView).toHaveBeenCalledWith(expect.objectContaining({ name: "Receivers", visibility: "private", filters: expect.objectContaining({ position: "WR", layout: "analyst" }) }), "staff"));
});
test("query errors provide a working retry instead of placeholder charts", async () => {
  vi.mocked(api.queryAnalytics).mockRejectedValueOnce(new Error("Server unavailable"));
  render(<TeamAnalytics />);
  fireEvent.click(await screen.findByRole("button", { name: "Retry analysis" }));
  await screen.findByRole("heading", { name: "Route depth" });
  expect(api.queryAnalytics).toHaveBeenCalledTimes(2);
});

test("a fully censored cohort shows supplied lower-bound evidence and opens its film", async () => {
  vi.mocked(api.queryAnalytics).mockResolvedValue({ ...result, groups: [], censoring: [{
    key: "all", metric_name: "ball_holding_duration", definition_version: "1", n: 1, events: 0, censored: 1, unit: "s",
    evidence: [{ ...row, value: 3, right_censored: true }],
    survival_curve: [{ seconds: 0, at_risk: 1, events: 0, censored: 0, survival: 1 }, { seconds: 3, at_risk: 1, events: 0, censored: 1, survival: 1 }],
  }] });
  render(<TeamAnalytics />);
  await screen.findByRole("heading", { name: /ball holding duration · observed endpoints/ });
  expect(screen.queryByText("No qualified measurements in this cohort")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Inspect duration" }));
  await screen.findByText(/Canonical play play-a/);
  expect(api.fetchPlayEvidence).toHaveBeenCalledWith("play-a", "staff");
});

test("definition failures have an explicit retry while film-linked measurements remain usable", async () => {
  vi.mocked(api.fetchMetricDefinitions).mockRejectedValueOnce(new Error("Definitions unavailable"));
  render(<TeamAnalytics />);
  fireEvent.click(await screen.findByRole("button", { name: "Retry definitions" }));
  await screen.findByRole("heading", { name: "Route depth" });
  expect(api.fetchMetricDefinitions).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("button", { name: "Retry definitions" })).toBeNull();
});

