import { afterEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const fixtures = vi.hoisted(() => {
  const recordings = [
    { id: "v-1", filename: "sideline.mp4", camera_label: "Sideline", status: "ready", practice_session_id: "s-1", recorded_at: "2026-10-14T12:00:00Z", created_at: "2026-10-14T12:00:00Z" },
    { id: "v-2", filename: "endzone.mp4", camera_label: "End zone", status: "ready", practice_session_id: "s-1", recorded_at: "2026-10-14T12:00:00Z", created_at: "2026-10-14T12:00:00Z" },
  ];
  const session = { id: "s-1", title: "Wednesday practice", session_kind: "practice", recorded_at: "2026-10-14T12:00:00Z", opponent_team: null, library_role: "toledo", notes: null, created_at: "2026-10-14T12:00:00Z", recordings, plays: [] };
  return { recordings, session, match: vi.fn() };
});

vi.mock("next/link", () => ({ default: ({ children, ...rest }: React.ComponentProps<"a">) => <a {...rest}>{children}</a> }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "coach-token", currentRole: "coach", data: { videos: fixtures.recordings }, refreshData: vi.fn() }) }));
vi.mock("@/components/shared/upload-widget", () => ({ useUploadWidget: () => ({ widget: null, openFilePicker: vi.fn() }) }));
vi.mock("@/lib/api", () => ({
  fetchSessions: vi.fn(async () => [fixtures.session]),
  fetchSession: vi.fn(async () => fixtures.session),
  fetchClipsForVideo: vi.fn(async (id: string) => [{ id: `c-${id}`, video_id: id, start_time: 1, end_time: 8, play_number: 12, is_reviewed: false }]),
  matchSessionPlays: fixtures.match,
  addSessionRecording: vi.fn(), createSession: vi.fn(),
}));

import { SessionWorkspace } from "./session-workspace";

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("session workspace", () => {
  test("each camera recording is watchable before matching views", async () => {
    render(<SessionWorkspace initialSessionId="s-1" onSelect={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByRole("link", { name: "Watch recording" })).toHaveLength(2));
    const links = screen.getAllByRole("link", { name: "Watch recording" });
    expect(links[0].getAttribute("href")).toContain("videoId=v-1");
    expect(links[1].getAttribute("href")).toContain("videoId=v-2");
    expect(screen.getByText("Sideline · 2026-10-14")).toBeTruthy();
    expect(screen.getByText("End zone · 2026-10-14")).toBeTruthy();
  });

  test("coach-verified views from two recordings are persisted as one play", async () => {
    fixtures.match.mockResolvedValueOnce({ ...fixtures.session, plays: [{ id: "p-1", session_id: "s-1", title: "Team rep 12", views: [{ id: "pv-1", clip_id: "c-v-1", video_id: "v-1", camera_label: "Sideline" }, { id: "pv-2", clip_id: "c-v-2", video_id: "v-2", camera_label: "End zone" }] }] });
    render(<SessionWorkspace initialSessionId="s-1" onSelect={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("Match play views")).toBeTruthy());
    fireEvent.click(screen.getByText("Match play views"));
    fireEvent.change(screen.getByLabelText("Play name, optional"), { target: { value: "Team rep 12" } });
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]);
    expect((screen.getByRole("button", { name: "Match selected views" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByRole("button", { name: "Match selected views" }));
    await waitFor(() => expect(fixtures.match).toHaveBeenCalledWith("s-1", { clip_ids: ["c-v-1", "c-v-2"], title: "Team rep 12" }, "coach-token"));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Team rep 12" })).toBeTruthy());
  });
});
