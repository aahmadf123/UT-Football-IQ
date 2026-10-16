import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { ApiError } from "@/lib/api-error";
import type { ApiClip } from "@/lib/types";

const { save, refresh } = vi.hoisted(() => ({ save: vi.fn(), refresh: vi.fn() }));
vi.mock("@/lib/api", () => ({ updateClip: save }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "token", currentRole: "coach", refreshData: refresh }) }));
import { ReviewMetadataPanel } from "./review-metadata-panel";

const clip: ApiClip = { id: "clip-1", video_id: "video-1", start_time: 4, end_time: 10, is_reviewed: false, annotation_revision: 2, created_at: "2026-10-14T12:00:00Z" };
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("play review", () => {
  test("saves tags and review status before navigating to the next play", async () => {
    save.mockResolvedValue({ ...clip, is_reviewed: true, annotation_revision: 3 });
    const next = vi.fn();
    const reload = vi.fn();
    render(<ReviewMetadataPanel clip={clip} onSaved={reload} onNext={next} />);
    fireEvent.change(screen.getByLabelText("Personnel"), { target: { value: "11" } });
    fireEvent.change(screen.getByLabelText("Down"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Distance (yards)"), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Save and next" }));
    await waitFor(() => expect(next).toHaveBeenCalledOnce());
    expect(save).toHaveBeenCalledWith("clip-1", expect.objectContaining({ personnel_grouping: "11", down: 3, distance: 7, is_reviewed: true, expected_revision: 2 }), "token");
    expect(reload).toHaveBeenCalledOnce();
  });

  test("a revision conflict preserves entered tags and does not advance", async () => {
    save.mockRejectedValue(new ApiError({ status: 409, action: "save the play", body: '{"detail":"stale annotation"}' }));
    const next = vi.fn();
    render(<ReviewMetadataPanel clip={clip} onSaved={() => {}} onNext={next} />);
    fireEvent.change(screen.getByLabelText("Personnel"), { target: { value: "12" } });
    fireEvent.click(screen.getByRole("button", { name: "Save and next" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("changed since you opened"));
    expect(next).not.toHaveBeenCalled();
    expect((screen.getByLabelText("Personnel") as HTMLInputElement).value).toBe("12");
  });
});
