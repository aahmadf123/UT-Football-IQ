import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { ClipBoundaryTools } from "./clip-boundary-tools";
import * as api from "@/lib/clip-edits-api";
import type { ApiClip } from "@/lib/types";
import { ApiError } from "@/lib/api-error";
const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }), useSearchParams: () => new URLSearchParams("clipId=one&position=OL&from=%2Fteam-stats%2F") }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "staff", currentRole: "coach" }) }));
vi.mock("@/lib/clip-edits-api", () => ({ fetchClipEdits: vi.fn(), splitClip: vi.fn(), mergeClips: vi.fn(), undoClipEdit: vi.fn() }));
const clip: ApiClip = { id: "one", video_id: "video", start_time: 100, end_time: 110, is_reviewed: false, annotation_revision: 3, created_at: "2026-10-21T12:00:00Z" };
beforeEach(() => {
  vi.mocked(api.fetchClipEdits).mockResolvedValue([]);
  vi.mocked(api.splitClip).mockResolvedValue({ operation_id: "split", operation_type: "split", clips: [{ ...clip, id: "result" }], superseded_clip_ids: ["one"], revisions: { one: 4, result: 0 }, undo_available: true });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

test("splitting uses the presented playhead on the original source clock and preserves context", async () => {
  const changed = vi.fn();
  render(<ClipBoundaryTools clip={clip} siblings={[clip]} readLocalTime={() => 2.5} onChanged={changed} />);
  fireEvent.click(screen.getByText("Split, merge and undo"));
  fireEvent.click(screen.getByRole("button", { name: "Use playhead" }));
  expect((screen.getByLabelText("Split at source time (seconds)") as HTMLInputElement).value).toBe("102.500");
  fireEvent.click(screen.getByRole("button", { name: "Split play" }));
  await waitFor(() => expect(api.splitClip).toHaveBeenCalledWith("one", 102.5, 3, "staff"));
  expect(changed).toHaveBeenCalledOnce();
  expect(push.mock.calls[0][0]).toContain("position=OL");
  expect(push.mock.calls[0][0]).toContain("clipId=result");
});
test("revision conflicts keep the current play and expose a recovery action", async () => {
  vi.mocked(api.splitClip).mockRejectedValueOnce(new ApiError({ status: 409, action: "edit boundaries", body: JSON.stringify({ detail: "This play changed; refresh before editing" }) }));
  render(<ClipBoundaryTools clip={clip} siblings={[clip]} readLocalTime={() => 2} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByText("Split, merge and undo"));
  fireEvent.click(screen.getByRole("button", { name: "Split play" }));
  await screen.findByRole("alert");
  expect(screen.getByText(/This play changed/)).toBeTruthy();
  expect(push).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Refresh edit history" })).toBeTruthy();
});

test("merge offers consecutive active plays and the split enforces the server's minimum", async () => {
  render(<ClipBoundaryTools clip={clip} siblings={[clip, { ...clip, id: "next", start_time: 112, end_time: 120 }, { ...clip, id: "later", start_time: 122, end_time: 130 }, { ...clip, id: "old", start_time: 110, end_time: 111, provenance: { state: "superseded" } }]} readLocalTime={() => 0} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByText("Split, merge and undo"));
  expect(screen.getByRole("option", { name: /112.0–120.0s/ })).toBeTruthy();
  expect(screen.queryByRole("option", { name: /122.0–130.0s/ })).toBeNull();
  expect(screen.queryByRole("option", { name: /110.0–111.0s/ })).toBeNull();
  fireEvent.change(screen.getByLabelText("Split at source time (seconds)"), { target: { value: "100.05" } });
  expect((screen.getByRole("button", { name: "Split play" }) as HTMLButtonElement).disabled).toBe(true);
});
