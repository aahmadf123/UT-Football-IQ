// Tracking Spotlight: the dashboard hero that plays the latest processed clip
// with real model overlays — and never fabricates boxes (repo rule #96).
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
});

async function importSpotlight() {
  vi.resetModules();
  const [{ CvSpotlight }, { AppStateProvider }] = await Promise.all([
    import("./cv-spotlight"),
    import("@/lib/app-state"),
  ]);
  return { CvSpotlight, AppStateProvider };
}

interface RouteSpec {
  url: string;
  body: unknown;
}

function installFetchRoutes(routes: RouteSpec[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const r of routes) {
      if (url.includes(r.url)) {
        return {
          ok: true,
          status: 200,
          json: async () => r.body,
          text: async () => JSON.stringify(r.body),
        } as unknown as Response;
      }
    }
    return {
      ok: true,
      status: 200,
      json: async () => [],
      text: async () => "[]",
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const READY_VIDEO = {
  id: "v-ready",
  filename: "PR_20260722_DRONEA.mp4",
  status: "ready",
  storage_uri: "s3://raw-video/PR_20260722_DRONEA.mp4",
  duration_seconds: 120,
  fps: 30,
  width: 1920,
  height: 1080,
  created_at: "2026-07-22T10:00:00Z",
};

const CLIP = {
  id: "c-1",
  video_id: "v-ready",
  start_time: 10,
  end_time: 16.5,
  play_number: 1,
  confidence: 0.9,
  is_reviewed: false,
  storage_uri: "s3://clips/c-1.mp4",
  created_at: "2026-07-22T10:05:00Z",
};

const OVERLAYS = {
  clip_id: "c-1",
  tracklets: [],
  events: [],
  labels: [],
  metrics: [],
  layers_available: { tracklets: true, events: false, labels: false, metrics: false },
  calibration: null,
};

describe("CvSpotlight", () => {
  test("plays the latest processed clip with live model overlays", async () => {
    installFetchRoutes([
      { url: "/api/v1/videos/v-ready/clips", body: [CLIP] },
      { url: "/api/v1/clips/c-1/overlays", body: OVERLAYS },
      { url: "download-url", body: { downloadUrl: "https://video.test/c-1.mp4" } },
      { url: "/api/v1/videos", body: [READY_VIDEO] },
    ]);

    const { CvSpotlight, AppStateProvider } = await importSpotlight();
    render(
      <AppStateProvider>
        <CvSpotlight />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("cv-spotlight-player")).toBeTruthy();
    });
    expect(screen.getByTestId("cv-spotlight-live").textContent).toContain("Model tracking live");
    expect(screen.getByTestId("cv-spotlight-clip-picker")).toBeTruthy();
    const reviewLink = screen.getByText(/Open in Clip Review/).closest("a");
    expect(reviewLink?.getAttribute("href")).toBe("/clip-review/?clipId=c-1");
    // The player is a real <video> aimed at the signed URL.
    const video = document.querySelector("video");
    expect(video?.getAttribute("src")).toBe("https://video.test/c-1.mp4");
  });

  test("processed video without detected plays says so honestly", async () => {
    installFetchRoutes([
      { url: "/api/v1/videos/v-ready/clips", body: [] },
      { url: "/api/v1/videos", body: [READY_VIDEO] },
    ]);

    const { CvSpotlight, AppStateProvider } = await importSpotlight();
    render(
      <AppStateProvider>
        <CvSpotlight />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText(/No plays detected yet/)).toBeTruthy();
    });
  });

  test("original-film spotlight seeks to the play, uses recording tracking time, and loops at its end", async () => {
    installFetchRoutes([
      { url: "/api/v1/videos/v-ready/clips", body: [CLIP] },
      { url: "/api/v1/clips/c-1/overlays", body: {
        ...OVERLAYS,
        tracks_timebase: "recording", source_start_time_seconds: 10, source_fps: 30,
        tracklets: [{ id: "t-source", player_id: null, start_frame: 300, end_frame: 495, team_label: "home", track_confidence: 0.9, track_points: [
          { frame_number: 300, timestamp_seconds: 10, bbox: [100, 200, 160, 320] },
          { frame_number: 315, timestamp_seconds: 10.5, bbox: [200, 200, 260, 320] },
          { frame_number: 495, timestamp_seconds: 16.5, bbox: [300, 200, 360, 320] },
        ] }],
        events: [{ id: "source-snap", event_type: "snap", frame_number: 315, timestamp_seconds: 10.5, timebase: "recording" }],
        layers_available: { tracklets: true, events: true, labels: false, metrics: false },
      } },
      { url: "download-url", body: { downloadUrl: "https://video.test/source.mp4" } },
      { url: "/api/v1/videos", body: [READY_VIDEO] },
    ]);
    const { CvSpotlight, AppStateProvider } = await importSpotlight();
    render(<AppStateProvider><CvSpotlight /></AppStateProvider>);
    await screen.findByTestId("cv-spotlight-player");
    const player = document.querySelector("video")!;
    fireEvent.loadedMetadata(player);
    expect(player.currentTime).toBe(10);
    player.currentTime = 10.5;
    fireEvent.timeUpdate(player);
    expect(screen.getByTestId("overlay-tracklet-t-source").querySelector("rect")?.getAttribute("x")).toBe("200");
    expect(screen.getByTestId("overlay-active-events").textContent).toContain("snap");
    player.currentTime = 17;
    fireEvent.timeUpdate(player);
    expect(player.currentTime).toBe(10);
    // The recording ending before a boundary callback still resumes the loop.
    const play = vi.fn().mockResolvedValue(undefined);
    player.play = play;
    Object.defineProperty(player, "ended", { configurable: true, value: true });
    player.currentTime = 16.9;
    fireEvent.ended(player);
    expect(player.currentTime).toBe(10);
    expect(play).toHaveBeenCalledOnce();
  });

  test("unsupported original film falls back to its clean clip and reports a second playback failure", async () => {
    const fetchMock = installFetchRoutes([
      { url: "/api/v1/videos/v-ready/clips", body: [CLIP] },
      { url: "/api/v1/clips/c-1/overlays", body: OVERLAYS },
      { url: "download-url?bucket=raw-video", body: { downloadUrl: "https://video.test/source.mov" } },
      { url: "download-url?bucket=clips", body: { downloadUrl: "https://video.test/clean-preview.mp4" } },
      { url: "/api/v1/videos", body: [READY_VIDEO] },
    ]);
    const { CvSpotlight, AppStateProvider } = await importSpotlight();
    render(<AppStateProvider><CvSpotlight /></AppStateProvider>);
    await screen.findByTestId("cv-spotlight-player");
    expect(document.querySelector("video")?.getAttribute("src")).toBe("https://video.test/source.mov");
    fireEvent.error(document.querySelector("video")!);
    await waitFor(() => expect(document.querySelector("video")?.getAttribute("src")).toBe("https://video.test/clean-preview.mp4"));
    const proxy = document.querySelector("video")!;
    Object.defineProperty(proxy, "videoWidth", { configurable: true, value: 960 });
    Object.defineProperty(proxy, "videoHeight", { configurable: true, value: 540 });
    fireEvent.loadedMetadata(proxy);
    expect(proxy.currentTime).toBe(0);
    expect(screen.getByTestId("clip-overlay-svg").getAttribute("viewBox")).toBe("0 0 1920 1080");
    fireEvent.error(proxy);
    expect(screen.getByRole("alert").textContent).toContain("Playback failed");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("download-url")).length).toBe(2);
  });

  test("no processed film yet renders an honest empty state", async () => {
    installFetchRoutes([
      { url: "/api/v1/videos", body: [{ ...READY_VIDEO, id: "v-2", status: "processing" }] },
    ]);

    const { CvSpotlight, AppStateProvider } = await importSpotlight();
    render(
      <AppStateProvider>
        <CvSpotlight />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("cv-spotlight-empty")).toBeTruthy();
    });
    expect(document.querySelector("video")).toBeNull();
  });

  test("mock mode never draws fabricated boxes", async () => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "1");
    installFetchRoutes([]);

    const { CvSpotlight, AppStateProvider } = await importSpotlight();
    render(
      <AppStateProvider>
        <CvSpotlight />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("cv-spotlight-mock")).toBeTruthy();
    });
    expect(document.querySelector("video")).toBeNull();
    expect(screen.queryByTestId("clip-overlay-svg")).toBeNull();
  });
});
