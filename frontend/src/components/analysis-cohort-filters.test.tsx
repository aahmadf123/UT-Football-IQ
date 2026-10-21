import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AnalysisCohortFilters } from "./analysis-cohort-filters";
import { ANALYSIS_DEFAULTS } from "@/lib/analysis-context";
import { fetchSessions } from "@/lib/api";
vi.mock("@/lib/api", () => ({ fetchSessions: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

test("named session selection and drill context update the same persistent filters", async () => {
  vi.mocked(fetchSessions).mockResolvedValue([{ id: "session-a", title: "Tuesday team", recorded_at: "2026-10-20", session_kind: "practice" } as Awaited<ReturnType<typeof fetchSessions>>[number]]);
  const onChange = vi.fn();
  render(<AnalysisCohortFilters context={ANALYSIS_DEFAULTS} onChange={onChange} token="staff" />);
  expect(fetchSessions).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Cohort details"));
  await screen.findByRole("option", { name: "Tuesday team · 2026-10-20 · practice" });
  fireEvent.change(screen.getByLabelText("Drill type"), { target: { value: "7on7" } });
  expect(onChange).toHaveBeenLastCalledWith({ drill_type: "7on7" });
  const sessions = screen.getByLabelText("Specific sessions") as HTMLSelectElement;
  sessions.options[0].selected = true;
  fireEvent.change(sessions);
  expect(onChange).toHaveBeenLastCalledWith({ session_ids: "session-a" });
});

test("older sessions can be loaded without losing a selected deep-link session", async () => {
  const sessions = Array.from({ length: 100 }, (_, index) => ({ id: `session-${index}`, title: `Practice ${index}`, recorded_at: null, session_kind: "practice" } as Awaited<ReturnType<typeof fetchSessions>>[number]));
  vi.mocked(fetchSessions).mockResolvedValueOnce(sessions).mockResolvedValueOnce([{ ...sessions[0], id: "older", title: "Older practice" }]);
  render(<AnalysisCohortFilters context={{ ...ANALYSIS_DEFAULTS, session_ids: "older" }} onChange={vi.fn()} token="staff" />);
  fireEvent.click(screen.getByText("Cohort details · 1 active"));
  fireEvent.click(await screen.findByRole("button", { name: "Load older sessions" }));
  const option = await screen.findByRole("option", { name: "Older practice · practice" });
  expect((option as HTMLOptionElement).selected).toBe(true);
  expect(fetchSessions).toHaveBeenLastCalledWith("staff", { limit: 100, offset: 100 });
});
