import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { sampleTeamDashboard } from "./team-dashboard.fixture";

vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => <a href={href} {...rest}>{children}</a>,
}));
const push = vi.fn();
vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ push }) }));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});
beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "http://api.test");
});

function serve(body: unknown) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      const payload = url.includes("/team/dashboard") ? body : [];
      return { ok: true, status: 200, json: async () => payload, text: async () => JSON.stringify(payload) } as Response;
    }),
  );
  return calls;
}

async function renderHome() {
  const [{ default: HomePage }, { AppStateProvider }] = await Promise.all([import("@/app/page"), import("@/lib/app-state")]);
  render(
    <AppStateProvider>
      <HomePage />
    </AppStateProvider>,
  );
}

describe("Home team dashboard", () => {
  test("leads with the latest week of film compared with the week before", async () => {
    serve(sampleTeamDashboard());
    await renderHome();
    const latest = await screen.findByRole("region", { name: "Latest week of film" });
    expect(latest.textContent).toMatch(/Latest film: week of Sep 7/);
    // Practice 230 + game 150 reps that week; previous week 210 + 140.
    expect(within(latest).getByText("380")).toBeTruthy();
    expect(within(latest).getAllByText(/\+30/).length).toBeGreaterThan(0);
    // Latest game's offense yards per play, compared with the previous game.
    expect(within(latest).getByText(/Michigan State/)).toBeTruthy();
    expect(within(latest).getByText("6.2")).toBeTruthy();
  });

  test("shows trends, position groups, games and readiness as charts with table views", async () => {
    serve(sampleTeamDashboard());
    await renderHome();
    expect(await screen.findByRole("img", { name: "Top speed, 90th percentile by week" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "Yards per play by game" })).toBeTruthy();
    expect(screen.getAllByText("Table view").length).toBeGreaterThan(3);
    expect(screen.getAllByText("WR").length).toBe(2); // position group card and readiness row
    expect(screen.getAllByText("One week so far").length).toBe(2); // OL has a single week
    expect(screen.getByRole("table", { name: "Mean workload risk by position group" })).toBeTruthy();
    expect(screen.getByText("has flagged player")).toBeTruthy();
    expect(screen.getByText(/Player-level flags are visible to sports performance staff/)).toBeTruthy();
  });

  test("filters scope the request", async () => {
    const calls = serve(sampleTeamDashboard());
    await renderHome();
    await screen.findByRole("region", { name: "Latest week of film" });
    fireEvent.click(screen.getByRole("radio", { name: "Games" }));
    await waitFor(() => expect(calls.some((url) => url.includes("session_kind=game"))).toBe(true));
    fireEvent.click(screen.getByRole("radio", { name: "4 weeks" }));
    await waitFor(() => expect(calls.some((url) => url.includes("weeks=4"))).toBe(true));
  });

  test("says what would fill an empty dashboard instead of inventing numbers", async () => {
    serve(sampleTeamDashboard({ weekly: [], position_groups: [], games: [], readiness: { visible: false, player_level: false, as_of: null, groups: [], flagged: [] } }));
    await renderHome();
    expect(await screen.findByText("No measured film in this range yet")).toBeTruthy();
    expect(screen.getByText(/No game film is linked/)).toBeTruthy();
    expect(screen.getByText(/visible to sports performance and analyst staff/)).toBeTruthy();
  });
});
