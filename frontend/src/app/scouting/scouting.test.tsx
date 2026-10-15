import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

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
  usePathname: () => "/scouting",
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
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
});

async function renderHub() {
  vi.resetModules();
  const [{ default: ScoutingPage }, { AppStateProvider }] = await Promise.all([
    import("./page"),
    import("@/lib/app-state"),
  ]);
  render(
    <AppStateProvider>
      <ScoutingPage />
    </AppStateProvider>,
  );
}

describe("ScoutingPage", () => {
  test("renders the three consolidated tabs", async () => {
    await renderHub();
    await waitFor(() => {
      expect(screen.getByTestId("scouting-tab-tendencies")).toBeTruthy();
    });
    expect(screen.getByTestId("scouting-tab-opponent")).toBeTruthy();
    expect(screen.getByTestId("scouting-tab-college")).toBeTruthy();
  });

  test("default tab is Our Tendencies (former Self-Scout)", async () => {
    await renderHub();
    await waitFor(() => {
      expect(screen.getByText(/Self-Scout filter/i)).toBeTruthy();
    });
    // Offline backend → unavailable cards, not fabricated numbers.
    await waitFor(() => {
      expect(screen.getAllByTestId("card-error").length).toBeGreaterThan(0);
    });
  });

  test("opponent tab renders the Opponent Prep view", async () => {
    nav.tab = "opponent";
    await renderHub();
    await waitFor(() => {
      expect(screen.getByText(/Opponent picker/i)).toBeTruthy();
    });
  });

  test("college tab renders the College Data (CFBD) view with an external-source label", async () => {
    nav.tab = "college";
    await renderHub();
    await waitFor(() => {
      expect(screen.getByTestId("cfbd-source-label").textContent).toContain(
        "CollegeFootballData.com",
      );
    });
    expect(screen.getByTestId("cfbd-banner").getAttribute("data-banner-tone")).toBe(
      "unavailable",
    );
  });
});
