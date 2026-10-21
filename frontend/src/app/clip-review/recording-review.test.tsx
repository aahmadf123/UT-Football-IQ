import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
const { video, download, clips, overlays, refreshDelay } = vi.hoisted(() => ({ video: vi.fn(), download: vi.fn(), clips: vi.fn().mockResolvedValue([]), overlays: vi.fn(), refreshDelay: vi.fn(() => 60_000) }));
vi.mock("@/lib/api", () => ({ fetchVideo: video, fetchVideoDownloadUrl: download, fetchClipsForVideo: clips, fetchClipOverlays: overlays, parseStorageUri: () => ({ bucket: "raw-video", key: "raw/test.mp4" }), signedUrlRefreshDelay: refreshDelay }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "token", data: { players: [] } }) }));
import { RecordingReview } from "./recording-review";
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); clips.mockResolvedValue([]); refreshDelay.mockReturnValue(60_000); });

describe("raw recording playback", () => {
  test("renews long-game URLs and restores time, speed and audio state", async () => {
    vi.useFakeTimers();
    refreshDelay.mockReturnValue(1000);
    video.mockResolvedValue({ id: "v1", filename: "game.mp4", duration_seconds: 5400, storage_uri: "local://raw-video/raw/test.mp4" });
    download.mockResolvedValueOnce("/media?sig=first").mockResolvedValue("/media?sig=renewed");
    render(<RecordingReview videoId="v1" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const element = screen.getByLabelText("game.mp4") as HTMLVideoElement;
    element.currentTime = 4500.25; element.playbackRate = 1.5; element.muted = true; element.volume = 0.4;
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    const renewed = screen.getByLabelText("game.mp4") as HTMLVideoElement;
    expect(renewed.getAttribute("src")).toContain("renewed");
    fireEvent.loadedMetadata(renewed);
    expect(renewed.currentTime).toBe(4500.25);
    expect(renewed.playbackRate).toBe(1.5);
    expect(renewed.muted).toBe(true);
    expect(renewed.volume).toBe(0.4);
    expect(download).toHaveBeenCalledWith("raw-video", "raw/test.mp4", "token", { fresh: true });
  });
  test("opens imported film before model inference or segmentation", async () => {
    video.mockResolvedValue({ id: "v1", filename: "practice.mp4", status: "uploaded", storage_uri: "local://raw-video/raw/test.mp4", practice_session_id: "session-1" });
    download.mockResolvedValue("/api/v1/storage/raw-video/raw/test.mp4?sig=test");
    render(<RecordingReview videoId="v1" />);
    const player = await screen.findByLabelText("practice.mp4");
    expect(player.getAttribute("src")).toContain("/api/v1/storage/");
    expect(screen.getByTestId("video-transport")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to session" }).getAttribute("href")).toContain("sessionId=session-1");
  });

  test("recovers a missing signed playback link after retry", async () => {
    video.mockResolvedValue({ id: "v1", filename: "practice.mp4", status: "uploaded", storage_uri: "local://raw-video/raw/test.mp4" });
    download.mockResolvedValueOnce(null).mockResolvedValueOnce("/api/v1/storage/raw-video/raw/test.mp4?sig=renewed");
    render(<RecordingReview videoId="v1" />);
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Refresh playback" }));
    await waitFor(() => expect(screen.getByLabelText("practice.mp4").getAttribute("src")).toContain("renewed"));
  });

  test("shows real tracking while the original recording plays through a detected play", async () => {
    video.mockResolvedValue({ id: "v1", filename: "practice.mp4", duration_seconds: 30, status: "ready", fps: 30, width: 1280, height: 720, storage_uri: "local://raw-video/raw/test.mp4" });
    download.mockResolvedValue("/api/v1/storage/raw-video/raw/test.mp4?sig=test");
    clips.mockResolvedValue([{ id: "c1", video_id: "v1", start_time: 10, end_time: 18, play_number: 1 }]);
    overlays.mockResolvedValue({ clip_id: "c1", tracks_timebase: "recording", source_start_time_seconds: 10, source_fps: 30, tracklets: [{ id: "t1", start_frame: 300, end_frame: 540, team_label: "home", player_id: null, track_confidence: 0.9, track_points: [{ frame_number: 300, timestamp_seconds: 10, bbox: [100, 100, 150, 200] }, { frame_number: 540, timestamp_seconds: 18, bbox: [200, 100, 250, 200] }] }], events: [], labels: [], metrics: [], layers_available: { tracklets: true, events: false, labels: false, metrics: false } });
    render(<RecordingReview videoId="v1" />);
    const player = await screen.findByLabelText("practice.mp4") as HTMLVideoElement;
    await screen.findByRole("button", { name: "Play 1" });
    fireEvent.click(screen.getByRole("button", { name: "Play 1" }));
    expect(player.currentTime).toBe(10);
    fireEvent.timeUpdate(player);
    expect(await screen.findByTestId("overlay-tracklet-t1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Player tracking on" }));
    expect(screen.queryByTestId("overlay-tracklet-t1")).toBeNull();
    player.currentTime = 20;
    fireEvent.timeUpdate(player);
    expect(screen.queryByTestId("clip-overlay-svg")).toBeNull();
  });
});
