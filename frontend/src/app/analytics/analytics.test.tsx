import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
const replace = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }), useSearchParams: () => new URLSearchParams("position=TE&season=2026&from=%2Fplayers%2F") }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
test("old analyst links retain filters and open analyst tools inside Team Analytics", async () => {
  const { default: Page } = await import("./page");
  render(<Page />);
  expect(screen.getByRole("status").textContent).toContain("Opening analyst tools");
  await waitFor(() => expect(replace).toHaveBeenCalled());
  const destination = new URL(replace.mock.calls[0][0], "https://film.test");
  expect(destination.pathname).toBe("/team-stats/");
  expect(destination.searchParams.get("position")).toBe("TE");
  expect(destination.searchParams.get("season")).toBe("2026");
  expect(destination.searchParams.get("layout")).toBe("analyst");
});
