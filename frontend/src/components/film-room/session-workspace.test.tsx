import { afterEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const fixtures = vi.hoisted(() => {
  const recordings = [
    { id: "v-1", filename: "sideline.mp4", camera_label: "Sideline", status: "ready", practice_session_id: "s-1", recorded_at: "2026-10-14T12:00:00Z", created_at: "2026-10-14T12:00:00Z" },
    { id: "v-2", filename: "endzone.mp4", camera_label: "End zone", status: "ready", practice_session_id: "s-1", recorded_at: "2026-10-14T12:00:00Z", created_at: "2026-10-14T12:00:00Z" },
  ];
  const session = { id: "s-1", title: "Wednesday practice", session_kind: "practice", recorded_at: "2026-10-14T12:00:00Z", opponent_team: null, library_role: "toledo", notes: null, created_at: "2026-10-14T12:00:00Z", recordings, plays: [] };
  return { recordings, session, match: vi.fn() };
});

vi.mock("next/link", () => ({ default: ({ children, ...rest }: React.ComponentProps<"a">) => <a {...rest}>{children}</a> }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "coach-token", currentRole: "coach", data: { videos: [fixtures.recordings[0]] }, refreshData: vi.fn() }) }));
vi.mock("@/components/shared/upload-widget", () => ({ useUploadWidget: () => ({ widget: null, openFilePicker: vi.fn() }) }));
vi.mock("@/app/library/library-view", () => ({ LibraryView: () => <div>Recording library</div> }));
vi.mock("@/lib/api", () => ({
  fetchAllSessions: vi.fn(async () => [fixtures.session]),
  fetchAllVideos: vi.fn(async () => []),
  fetchSession: vi.fn(async () => fixtures.session),
  fetchClipsForVideo: vi.fn(async (id: string) => [{ id: `c-${id}`, video_id: id, start_time: 1, end_time: 8, play_number: 12, is_reviewed: false }]),
  matchSessionPlays: fixtures.match,
  addSessionRecording: vi.fn(), createSession: vi.fn(),
}));

import * as api from "@/lib/api";
import type { ApiClip, ApiSession, ApiVideo } from "@/lib/types";
import { SessionWorkspace } from "./session-workspace";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.mocked(api.fetchSession).mockReset().mockResolvedValue(fixtures.session as ApiSession);
  vi.mocked(api.fetchAllSessions).mockReset().mockResolvedValue([fixtures.session as ApiSession]);
  vi.mocked(api.fetchAllVideos).mockReset().mockResolvedValue([]);
});

describe("session workspace", () => {
  test("recording grouping uses the full catalog and explains membership exclusions", async () => {
    const available = Array.from({ length: 30 }, (_, index) => ({ ...fixtures.recordings[0], id: `ungrouped-${index}`, filename: `Full library recording ${index}`, practice_session_id: null }));
    vi.mocked(api.fetchAllVideos).mockResolvedValueOnce([...available, fixtures.recordings[0], { ...fixtures.recordings[1], id: "other-session", practice_session_id: "s-other" }] as ApiVideo[]);
    render(<SessionWorkspace initialSessionId="s-1" onSelect={vi.fn()} />);
    await screen.findByRole("heading", { name: "Wednesday practice" });
    fireEvent.click(screen.getByText("Group a recording already in your library"));
    await screen.findByText("32 uploaded · 30 available to add");
    expect(api.fetchAllVideos).toHaveBeenCalledWith({}, "coach-token");
    const picker = within(screen.getByLabelText("Recording"));
    expect(picker.getAllByRole("option")).toHaveLength(31);
    expect(picker.getByRole("option", { name: "Full library recording 29" })).toBeTruthy();
    expect(picker.queryByRole("option", { name: "sideline.mp4" })).toBeNull();
    expect(screen.getByText(/Already in this session: 1.*Grouped in other sessions: 1/)).toBeTruthy();
  });

  test("recording catalog failure is separate from session loading and can be retried", async () => {
    vi.mocked(api.fetchAllVideos).mockRejectedValueOnce(new Error("Recording connection interrupted"));
    render(<SessionWorkspace initialSessionId="s-1" onSelect={vi.fn()} />);
    await screen.findByRole("heading", { name: "Wednesday practice" });
    fireEvent.click(screen.getByText("Group a recording already in your library"));
    await screen.findByText(/Uploaded recordings could not be loaded/);
    expect((screen.getByLabelText("Recording") as HTMLSelectElement).disabled).toBe(true);
    vi.mocked(api.fetchAllVideos).mockResolvedValueOnce([{ ...fixtures.recordings[0], id: "elsewhere", practice_session_id: "s-other" }] as ApiVideo[]);
    fireEvent.click(screen.getByRole("button", { name: "Retry uploaded recordings" }));
    await screen.findByText("1 uploaded · 0 available to add");
    expect(screen.getByText(/All uploaded recordings are already grouped/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Review all uploaded recordings" }).getAttribute("href")).toBe("/film-room/?tab=review");
    expect(screen.getByRole("heading", { name: "Wednesday practice" })).toBeTruthy();
  });

  test("practice creation never asks for an opponent or retains a game opponent draft", async () => {
    vi.mocked(api.createSession).mockResolvedValueOnce(fixtures.session as ApiSession);
    render(<SessionWorkspace onSelect={vi.fn()} />);
    await waitFor(() => expect((screen.getByLabelText("Session") as HTMLSelectElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(screen.queryByLabelText("Opponent, if applicable")).toBeNull();
    fireEvent.change(screen.getByLabelText("Session name"), { target: { value: "Thursday team period" } });
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "game" } });
    fireEvent.change(screen.getByLabelText("Opponent, if applicable"), { target: { value: "Game opponent" } });
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "practice" } });
    expect(screen.queryByLabelText("Opponent, if applicable")).toBeNull();
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "game" } });
    expect((screen.getByLabelText("Opponent, if applicable") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "practice" } });
    fireEvent.click(screen.getByRole("button", { name: "Create session" }));
    await waitFor(() => expect(api.createSession).toHaveBeenCalledWith({ title: "Thursday team period", session_kind: "practice", library_role: "toledo" }, "coach-token"));
  });

  test("local session choice follows subsequent URL changes and Back without restoring the old choice", async () => {
    const second = { ...fixtures.session, id: "s-2", title: "Thursday practice" } as ApiSession;
    vi.mocked(api.fetchAllSessions).mockResolvedValue([fixtures.session as ApiSession, second]);
    vi.mocked(api.fetchSession).mockImplementation(async (id) => id === "s-2" ? second : fixtures.session as ApiSession);
    const onSelect = vi.fn();
    const { rerender } = render(<SessionWorkspace initialSessionId="s-1" onSelect={onSelect} />);
    await screen.findByRole("heading", { name: "Wednesday practice" });
    fireEvent.change(screen.getByLabelText("Session"), { target: { value: "s-2" } });
    await screen.findByRole("heading", { name: "Thursday practice" });
    expect(onSelect).toHaveBeenCalledWith("s-2");
    rerender(<SessionWorkspace initialSessionId="s-2" onSelect={onSelect} />);
    rerender(<SessionWorkspace initialSessionId="s-1" onSelect={onSelect} />);
    await screen.findByRole("heading", { name: "Wednesday practice" });
    expect((screen.getByLabelText("Session") as HTMLSelectElement).value).toBe("s-1");
    rerender(<SessionWorkspace initialSessionId="" onSelect={onSelect} />);
    await screen.findByRole("region", { name: "Saved sessions" });
    expect(screen.getByRole("button", { name: /Thursday practice/ })).toBeTruthy();
  });

  test("sessions beyond the first hundred remain selectable from the complete catalog", async () => {
    const rows = Array.from({ length: 101 }, (_, index) => ({ ...fixtures.session, id: `catalog-${index}`, title: `Saved practice ${index}`, recordings: [] })) as ApiSession[];
    vi.mocked(api.fetchAllSessions).mockResolvedValueOnce(rows);
    vi.mocked(api.fetchSession).mockResolvedValueOnce(rows[100]);
    render(<SessionWorkspace onSelect={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(102));
    expect(api.fetchAllSessions).toHaveBeenCalledWith("coach-token");
    fireEvent.change(screen.getByLabelText("Session"), { target: { value: "catalog-100" } });
    await screen.findByRole("heading", { name: "Saved practice 100" });
  });

  test("prop navigation follows the requested session and ignores a late previous-session response", async () => {
    let resolveOld!: (value: ApiSession) => void;
    const old = new Promise<ApiSession>((resolve) => { resolveOld = resolve; });
    const second = { ...fixtures.session, id: "s-2", title: "Thursday practice", recordings: [{ ...fixtures.recordings[1], id: "v-thursday", filename: "Thursday endzone.mp4" }] } as ApiSession;
    vi.mocked(api.fetchAllSessions).mockResolvedValueOnce([fixtures.session as ApiSession, second]);
    vi.mocked(api.fetchSession).mockImplementationOnce(() => old).mockResolvedValueOnce(second);
    const onSelect = vi.fn();
    const { rerender } = render(<SessionWorkspace initialSessionId="s-1" onSelect={onSelect} />);
    await waitFor(() => expect(api.fetchSession).toHaveBeenCalledWith("s-1", "coach-token"));
    rerender(<SessionWorkspace initialSessionId="s-2" onSelect={onSelect} />);
    await screen.findByRole("heading", { name: "Thursday practice" });
    await act(async () => { resolveOld(fixtures.session as ApiSession); });
    expect(screen.queryByRole("heading", { name: "Wednesday practice" })).toBeNull();
    expect(screen.getByRole("link", { name: "Full recording" }).getAttribute("href")).toContain("videoId=v-thursday");
    rerender(<SessionWorkspace initialSessionId="" onSelect={onSelect} />);
    expect(await screen.findByRole("region", { name: "Saved sessions" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Wednesday practice/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Thursday practice/ })).toBeTruthy();
    expect(onSelect).not.toHaveBeenCalled();
  });

  test("a later poll cannot be overwritten by an earlier response from the same session", async () => {
    let resolveOlder!: (value: ApiSession) => void;
    const older = new Promise<ApiSession>((resolve) => { resolveOlder = resolve; });
    vi.mocked(api.fetchSession).mockImplementationOnce(() => older).mockResolvedValueOnce({ ...fixtures.session, title: "Current recording catalog" } as ApiSession);
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      render(<SessionWorkspace initialSessionId="s-1" onSelect={vi.fn()} />);
      await waitFor(() => expect(api.fetchSession).toHaveBeenCalledTimes(1));
      await act(async () => { vi.advanceTimersByTime(5000); });
      await screen.findByRole("heading", { name: "Current recording catalog" });
      await act(async () => { resolveOlder(fixtures.session as ApiSession); });
      expect(screen.getByRole("heading", { name: "Current recording catalog" })).toBeTruthy();
      expect(screen.queryByRole("heading", { name: "Wednesday practice" })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  test("each camera recording is watchable before matching views", async () => {
    render(<SessionWorkspace initialSessionId="s-1" onSelect={vi.fn()} />);
    await waitFor(() => expect(screen.getAllByRole("link", { name: "Full recording" })).toHaveLength(2));
    const links = screen.getAllByRole("link", { name: "Full recording" });
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


test("unresolved source windows stay watchable with a source-window label", async () => {
  vi.mocked(api.fetchSession).mockResolvedValueOnce({ ...fixtures.session, play_count: 0, source_window_count: 1, plays: [{ id: "window", count_eligible: false, sequence: 1, views: [{ id: "view", clip_id: "c-v-1", video_id: "v-1", camera_label: "Sideline" }] }] } as unknown as ApiSession);
  vi.mocked(api.fetchClipsForVideo).mockImplementationOnce(async () => [{ id: "c-v-1", video_id: "v-1", start_time: 0, end_time: 8, play_number: 1, is_reviewed: false, created_at: "2026-10-21T00:00:00Z", label_data: { canonical_count_eligible: false } } as ApiClip]);
  render(<SessionWorkspace initialSessionId="s-1" onSelect={vi.fn()} />);
  await screen.findByRole("heading", { name: "Source window 1" });
  expect(screen.getByRole("link", { name: /^Sideline$/ }).getAttribute("href")).toContain("clipId=c-v-1");
});
