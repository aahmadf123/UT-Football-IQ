import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
const { video, download } = vi.hoisted(() => ({ video: vi.fn(), download: vi.fn() }));
vi.mock("@/lib/api", () => ({ fetchVideo: video, fetchVideoDownloadUrl: download, parseStorageUri: () => ({ bucket: "raw-video", key: "raw/test.mp4" }) }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "token" }) }));
import { RecordingReview } from "./recording-review";
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("raw recording playback", () => {
  test("opens imported film before model inference or segmentation", async () => {
    video.mockResolvedValue({ id: "v1", filename: "practice.mp4", status: "uploaded", storage_uri: "local://raw-video/raw/test.mp4", practice_session_id: "session-1" });
    download.mockResolvedValue("/api/v1/storage/raw-video/raw/test.mp4?sig=test");
    render(<RecordingReview videoId="v1" />);
    const player = await screen.findByLabelText("practice.mp4");
    expect(player.getAttribute("src")).toContain("/api/v1/storage/");
    expect(player.hasAttribute("controls")).toBe(true);
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
});
