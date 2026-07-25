// Role-gating tests for the Health & Workload surface (Issue #113).
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ children, ...rest }: React.ComponentProps<"a">) => <a {...rest}>{children}</a>,
}));

vi.mock("next/image", () => ({
  default: () => null,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/health-workload",
  useSearchParams: () => new URLSearchParams(""),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.resetModules();
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "");
});

async function renderPage() {
  vi.resetModules();
  const [{ default: HealthWorkloadPage }, { AppStateProvider }] = await Promise.all([
    import("./page"),
    import("@/lib/app-state"),
  ]);
  render(
    <AppStateProvider>
      <HealthWorkloadPage />
    </AppStateProvider>,
  );
}

describe("Health & Workload surface gating", () => {
  test("approved role (sportsperformance) sees the surface and disclaimer", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "sportsperformance");
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-workload-surface")).toBeTruthy();
    });
    // Non-medical disclaimer is shown verbatim.
    const disclaimer = screen.getByTestId("health-workload-disclaimer");
    expect(disclaimer.textContent).toContain("not a medical device");
    expect(disclaimer.textContent).toContain("predict injury risk");
    // All three placeholder integrations render, marked Not connected.
    expect(screen.getByTestId("hw-integration-wellness").textContent).toContain("Not connected");
    expect(screen.getByTestId("hw-integration-gps_wearables")).toBeTruthy();
    expect(screen.getByTestId("hw-integration-strength_conditioning")).toBeTruthy();
    // No restricted notice for an approved role.
    expect(screen.queryByTestId("health-workload-restricted")).toBeNull();
    // Nav exposes the Health & Workload entry for approved roles.
    const nav = screen.getByRole("navigation", { name: /Football IQ navigation/i });
    expect(within(nav).queryByText("Health & Workload")).toBeTruthy();
  });

  test("non-approved role (coach) is gated and the nav entry is hidden", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "coach");
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-workload-restricted")).toBeTruthy();
    });
    // The surface content is not rendered for a non-approved role.
    expect(screen.queryByTestId("health-workload-surface")).toBeNull();
    expect(screen.queryByTestId("health-workload-disclaimer")).toBeNull();
    // Nav hides the Health & Workload entry (the page <h1> title still exists,
    // so we scope the assertion to the navigation landmark).
    const nav = screen.getByRole("navigation", { name: /Football IQ navigation/i });
    expect(within(nav).queryByText("Health & Workload")).toBeNull();
  });

  test("default role (no token, no override) is gated", async () => {
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-workload-restricted")).toBeTruthy();
    });
    expect(screen.queryByTestId("health-workload-surface")).toBeNull();
  });
});

describe("Daily athlete state dashboard (Issue #9)", () => {
  test("renders the panel and the policy statement for sportsperformance", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "sportsperformance");
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-daily-state")).toBeTruthy();
    });
    const policy = screen.getByTestId("health-policy-statement");
    expect(policy.textContent).toContain("support staff judgment");
    expect(policy.textContent).toContain("not a medical diagnosis");
  });

  test("panel is absent for gated roles (coach)", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "coach");
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-workload-restricted")).toBeTruthy();
    });
    expect(screen.queryByTestId("health-daily-state")).toBeNull();
    expect(screen.queryByTestId("health-policy-statement")).toBeNull();
  });
});

describe("Workload risk signals panel (Issue #149)", () => {
  test("renders with the non-diagnostic caveat for sportsperformance", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "sportsperformance");
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-workload-risk")).toBeTruthy();
    });
    const caveat = screen.getByTestId("workload-risk-caveat");
    expect(caveat.textContent).toContain("not a diagnosis");
    expect(caveat.textContent).toContain("Experimental");
  });

  test("renders for analyst (aggregate-only role) without player-level table", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "analyst");
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-workload-risk")).toBeTruthy();
    });
    // No auth token in this test setup → panel is idle; the player-level
    // table must never render for analyst regardless of state.
    expect(screen.queryByTestId("workload-risk-table")).toBeNull();
  });

  test("panel is absent entirely for gated roles", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "coach");
    await renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("health-workload-restricted")).toBeTruthy();
    });
    expect(screen.queryByTestId("health-workload-risk")).toBeNull();
  });
});
