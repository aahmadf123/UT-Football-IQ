import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ children, href, onClick }: { children: React.ReactNode; href: string; onClick?: React.MouseEventHandler<HTMLAnchorElement> }) => (
    <a href={href} onClick={onClick}>{children}</a>
  ),
}));

vi.mock("next/image", () => ({
  default: () => null,
}));

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/clip-review",
  useSearchParams: () => new URLSearchParams(window.location.search || "clipId=c-1"),
  useRouter: () => ({ push: routerPush, replace: vi.fn(), back: vi.fn() }),
}));

afterEach(() => {
  cleanup();
  window.history.replaceState({}, "", "/");
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

test("clean proxy transport steps at proxy fps while overlays use source fps", async () => {
  installFetchRoutes([
    { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/proxy" } },
    { url: "/api/v1/clips/c-1/overlays", body: { ...EMPTY_OVERLAYS, source_fps: 60 } },
    { url: "/api/v1/clips/c-1", body: { ...SAMPLE_CLIP, provenance: { playback: { kind: "clean_clip", proxy_fps: 30 } } } },
    { url: "/api/v1/videos/v-1", body: { ...SAMPLE_VIDEO, fps: 60 } },
    { url: "/api/v1/videos/v-1/clips", body: [SAMPLE_CLIP] },
    { url: "/api/v1/players", body: [] },
    { url: "/api/v1/inbox/status", body: [] },
    { url: "/api/v1/videos", body: [] },
    { url: "/api/v1/jobs", body: [] },
    { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
  ]);
  const { ClipReviewPage, AppStateProvider } = await importPage();
  render(<AppStateProvider><ClipReviewPage /></AppStateProvider>);
  await screen.findByTestId("video-transport");

  const player = document.querySelector("video")!;
  fireEvent.click(screen.getByTestId("transport-frame-forward"));
  expect(player.currentTime).toBe(0.05);
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
});

async function importPage() {
  vi.resetModules();
  const [{ default: ClipReviewPage }, { AppStateProvider }] = await Promise.all([
    import("./page"),
    import("@/lib/app-state"),
  ]);
  return { ClipReviewPage, AppStateProvider };
}

const SAMPLE_CLIP = {
  id: "c-1",
  video_id: "v-1",
  start_time: 10,
  end_time: 18,
  play_number: 7,
  is_reviewed: false,
  storage_uri: "s3://clips/c-1.mp4",
};

const SAMPLE_VIDEO = {
  id: "v-1",
  filename: "practice.mp4",
  status: "ready",
  fps: 30,
  width: 1920,
  height: 1080,
  storage_uri: "s3://raw/v-1.mp4",
  session_kind: "practice",
};

const EMPTY_OVERLAYS = {
  clip_id: "c-1",
  tracklets: [],
  events: [],
  labels: [],
  metrics: [],
  layers_available: {
    tracklets: false,
    events: false,
    labels: false,
    metrics: false,
  },
};

interface RouteSpec {
  url: string | RegExp;
  body: unknown;
  status?: number;
  ok?: boolean;
}

function installFetchRoutes(routes: RouteSpec[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    for (const r of routes) {
      const matches =
        typeof r.url === "string" ? url.includes(r.url) : r.url.test(url);
      if (matches) {
        const ok = r.ok ?? true;
        const status = r.status ?? (ok ? 200 : 500);
        return {
          ok,
          status,
          json: async () => r.body,
          text: async () => JSON.stringify(r.body),
        } as unknown as Response;
      }
    }
    return {
      ok: false,
      status: 404,
      json: async () => ({ detail: "not mocked" }),
      text: async () => "not mocked",
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("ClipReviewPage overlays", () => {
  test("opening a saved workspace on the same clip restores media and inspector state", async () => {
    installFetchRoutes([
      { url: "/api/v1/saved-views", body: [{ id: "view-1", name: "Saved rep", surface: "film", visibility: "private", filters: { clipId: "c-1", t: "3", rate: "0.75", inspector: "hidden", playSearch: "7" } }] },
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/proxy" } },
      { url: "/api/v1/clips/c-1/overlays", body: EMPTY_OVERLAYS },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1/clips", body: [SAMPLE_CLIP] },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/videos", body: [] }, { url: "/api/v1/jobs", body: [] }, { url: "/api/v1/players", body: [] }, { url: "/api/v1/inbox/status", body: [] },
    ]);
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(<AppStateProvider><ClipReviewPage /></AppStateProvider>);
    await screen.findByTestId("video-transport");
    const element = document.querySelector("video")!;
    element.currentTime = SAMPLE_CLIP.start_time + 4;
    element.playbackRate = 1.5;
    const details = screen.getByText("Saved film workspaces").parentElement as HTMLDetailsElement;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    await screen.findByRole("option", { name: "Saved rep · private" });
    fireEvent.change(screen.getByLabelText("Open workspace"), { target: { value: "view-1" } });
    expect(element.currentTime).toBe(SAMPLE_CLIP.start_time + 3);
    expect(element.playbackRate).toBe(0.75);
    expect((screen.getByLabelText("Find a play") as HTMLInputElement).value).toBe("7");
    expect(screen.getByRole("button", { name: "Show inspector" })).toBeTruthy();
  });
  test("switching camera angles replaces the current clip in its playlist", async () => {
    window.history.replaceState({}, "", "/clip-review/?clipId=c-1&playlist=c-1%2Cc-3");
    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: EMPTY_OVERLAYS },
      { url: "/api/v1/clips/c-1", body: { ...SAMPLE_CLIP, label_data: { view: "sideline" } } },
      {
        url: "/api/v1/videos/v-1/clips",
        body: [
          { ...SAMPLE_CLIP, label_data: { view: "sideline" } },
          {
            ...SAMPLE_CLIP,
            id: "c-angle",
            label_data: { view: "endzone" },
          },
        ],
      },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    const angleLink = await screen.findByRole("link", { name: "End zone" });
    expect(angleLink.getAttribute("href")).toBe(
      "/clip-review/?clipId=c-angle&playlist=c-angle%2Cc-3",
    );
    const element = document.querySelector("video")!;
    element.currentTime = SAMPLE_CLIP.start_time + 2.25;
    element.playbackRate = 1.5;
    fireEvent.click(angleLink);
    const destination = new URL(routerPush.mock.calls.at(-1)![0], "https://film.test");
    expect(destination.searchParams.get("t")).toBe("2.25");
    expect(destination.searchParams.get("rate")).toBe("1.5");
    expect(destination.searchParams.get("playlist")).toBe("c-angle,c-3");
  });

  test("explains a local API network failure", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );
    await waitFor(() => {
      expect(screen.getByText(/Could not load clip/i)).toBeTruthy();
    });
  });

  test("renders overlay summary with empty state when backend returns empty layers", async () => {
    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: EMPTY_OVERLAYS },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("overlay-empty")).toBeTruthy();
    });
    expect(screen.getByTestId("overlay-toggles")).toBeTruthy();
    // All data toggles should be disabled because no layer has data.
    expect(
      (screen.getByTestId("overlay-toggle-tracks") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.getByTestId("event-timeline-empty")).toBeTruthy();
  });

  test("renders SVG overlay and timeline markers when overlays are present", async () => {
    const payload = {
      clip_id: "c-1",
      tracklets: [
        {
          id: "t-1",
          player_id: null,
          start_frame: 0,
          end_frame: 300,
          track_confidence: 0.9,
          team_label: "home",
          position_group: "Skill",
          side_of_ball: "offense",
          // Camera overlays draw from bboxes only; field coords feed the
          // separate Field-view mini-map.
          track_points: [
            {
              frame_number: 0,
              field_x: 30,
              field_y: 25,
              bbox: [100, 200, 160, 320],
              detection_confidence: 0.9,
            },
            {
              frame_number: 15,
              field_x: 35,
              field_y: 24,
              bbox: [130, 210, 190, 330],
              detection_confidence: 0.9,
            },
          ],
        },
      ],
      events: [
        {
          id: "e-1",
          event_type: "snap",
          frame_number: 15,
          timestamp_seconds: 0.5,
          attributes: null,
        },
      ],
      labels: [
        {
          id: "lb-1",
          tracklet_id: null,
          label_type: "offensive_formation",
          label_value: { name: "trips_right" },
          source: "model",
          qualification: "model_approved" as const,
        },
        { id: "lb-human", tracklet_id: null, label_type: "coverage_shell", label_value: { coverage: "reviewed-contract" }, source: "human", qualification: "human_reviewed" as const },
        { id: "lb-legacy", tracklet_id: null, label_type: "route_family", label_value: { route: "legacy-contract" }, source: "model" },
      ],
      withheld_label_count: 2,
      warnings: ["2 unapproved or unverified model/annotation labels are withheld. Original labels remain available in Learning and annotation history."],
      metrics: [
        {
          id: "m-1",
          tracklet_id: null,
          metric_name: "yards_gained",
          metric_value: { value: 7 },
          unit: "yards",
          confidence: 0.92,
        },
      ],
      layers_available: { tracklets: true, events: true, labels: true, metrics: true },
    };

    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: payload },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("overlay-summary")).toBeTruthy();
    });
    expect(screen.getByTestId("clip-overlay-svg")).toBeTruthy();
    expect(screen.getByTestId("overlay-tracklet-t-1")).toBeTruthy();
    expect(screen.getByTestId("event-marker-e-1")).toBeTruthy();
    expect(screen.getByText(/trips_right/)).toBeTruthy();
    expect(screen.getByText("Approved model output")).toBeTruthy();
    expect(screen.getByText("Staff reviewed annotation")).toBeTruthy();
    expect(screen.getByText("Unverified annotation")).toBeTruthy();
    expect(screen.getByText(/2 unapproved or unverified/)).toBeTruthy();
    expect(screen.getByText(/yards_gained/)).toBeTruthy();

    // Toggling "raw" hides the overlay SVG.
    fireEvent.click(screen.getByTestId("overlay-toggle-raw"));
    await waitFor(() => {
      expect(screen.queryByTestId("overlay-tracklet-t-1")).toBeNull();
    });
  });

  test("recording-time tracks and mixed event clocks stay aligned with a later play", async () => {
    const fetchMock = installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: {
        ...EMPTY_OVERLAYS,
        tracks_timebase: "recording",
        source_start_time_seconds: 10,
        source_fps: 60,
        tracklets: [{ id: "recording-track", player_id: null, start_frame: 600, end_frame: 1080, team_label: "home", track_confidence: 0.94, track_points: [
          { frame_number: 600, timestamp_seconds: 10, bbox: [100, 200, 160, 320] },
          { frame_number: 660, timestamp_seconds: 11, bbox: [200, 200, 260, 320] },
          { frame_number: 1080, timestamp_seconds: 18, bbox: [300, 200, 360, 320] },
        ] }],
        events: [
          { id: "recording-snap", event_type: "snap", frame_number: 660, timestamp_seconds: null, timebase: "recording" },
          { id: "manual-event", event_type: "throw", frame_number: null, timestamp_seconds: 2, timebase: "clip" },
          { id: "unknown-event", event_type: "unknown", frame_number: 720, timestamp_seconds: 12, timebase: "unknown" },
          { id: "outside-clip", event_type: "late", frame_number: 1500, timestamp_seconds: 25, timebase: "recording" },
        ],
        layers_available: { tracklets: true, events: true, labels: false, metrics: false },
      } },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1/clips", body: [SAMPLE_CLIP] },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(<AppStateProvider><ClipReviewPage /></AppStateProvider>);
    await screen.findByTestId("overlay-tracklet-recording-track");
    const player = document.querySelector("video")!;
    fireEvent.click(screen.getByTestId("event-marker-recording-snap"));
    expect(player.currentTime).toBe(11);
    fireEvent.timeUpdate(player);
    expect(screen.getByTestId("overlay-tracklet-recording-track").querySelector("rect")?.getAttribute("x")).toBe("200");
    expect(screen.getByTestId("overlay-active-events").textContent).toContain("snap");
    expect(screen.queryByTestId("event-marker-unknown-event")).toBeNull();
    expect(screen.queryByTestId("event-marker-outside-clip")).toBeNull();
    fireEvent.click(screen.getByTestId("event-marker-manual-event"));
    expect(player.currentTime).toBe(12);
    fireEvent.click(screen.getByRole("tab", { name: "Players" }));
    fireEvent.click(screen.getByRole("button", { name: "Inspect track T1" }));
    expect(screen.getByTestId("selected-player-inspector").textContent).toContain("94%");
    fireEvent.keyDown(screen.getByTestId("event-timeline"), { key: "Home" });
    expect(player.currentTime).toBe(10);
    // A browser-compatible proxy is smaller than the source recording, but
    // the detector's boxes remain in the original 1920 × 1080 coordinates.
    fireEvent.error(player);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("videos/download-url")).length).toBeGreaterThanOrEqual(2));
    Object.defineProperty(player, "videoWidth", { configurable: true, value: 960 });
    Object.defineProperty(player, "videoHeight", { configurable: true, value: 540 });
    player.currentTime = 0;
    fireEvent.loadedMetadata(player);
    fireEvent.timeUpdate(player);
    expect(screen.getByTestId("clip-overlay-svg").getAttribute("viewBox")).toBe("0 0 1920 1080");
    expect(screen.getByTestId("overlay-tracklet-recording-track").querySelector("rect")?.getAttribute("x")).toBe("100");
  });

  test("transport renders, timeline seeks, and next-play navigates", async () => {
    routerPush.mockClear();
    const payload = {
      clip_id: "c-1",
      tracklets: [],
      events: [
        { id: "e-1", event_type: "snap", frame_number: 15, timestamp_seconds: 0.5, attributes: null },
      ],
      labels: [],
      metrics: [],
      layers_available: { tracklets: false, events: true, labels: false, metrics: false },
    };
    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: payload },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      {
        url: "/api/v1/videos/v-1/clips",
        body: [
          SAMPLE_CLIP,
          { ...SAMPLE_CLIP, id: "c-2", start_time: 20, end_time: 26, play_number: 8 },
        ],
      },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("video-transport")).toBeTruthy();
    });
    // Native controls are replaced by the transport.
    const video = document.querySelector("video")!;
    expect(video.hasAttribute("controls")).toBe(false);

    // Source-film playback shifts the clip-local event onto the recording timeline.
    fireEvent.click(screen.getByTestId("event-marker-e-1"));
    expect(video.currentTime).toBeCloseTo(SAMPLE_CLIP.start_time + 0.5, 3);
    fireEvent.timeUpdate(video);
    await waitFor(() => expect(screen.getByTestId("transport-readout").textContent).toContain("0.50s"));

    // Frame stepping moves one frame at the video's own fps (30).
    fireEvent.click(screen.getByTestId("transport-frame-forward"));
    expect(video.currentTime).toBeCloseTo(SAMPLE_CLIP.start_time + (16 + 0.5) / 30, 3);

    // Prev is disabled on the first play; Next navigates to the sibling clip.
    await waitFor(() => {
      expect(
        (screen.getByTestId("transport-next-clip") as HTMLButtonElement).disabled,
      ).toBe(false);
    });
    expect((screen.getByTestId("transport-prev-clip") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("transport-next-clip"));
    expect(routerPush).toHaveBeenCalledWith("/clip-review/?clipId=c-2");

    // Keyboard: "]" also advances to the next play.
    routerPush.mockClear();
    fireEvent.keyDown(document, { key: "]" });
    expect(routerPush).toHaveBeenCalledWith("/clip-review/?clipId=c-2");
  });

  test("playback reports an error after both sources fail instead of alternating", async () => {
    const fetchMock = installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: EMPTY_OVERLAYS },
      { url: "/api/v1/clips/c-1", body: { ...SAMPLE_CLIP, provenance: { playback: { kind: "clean_clip" } } } },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/videos/v-1/clips", body: [SAMPLE_CLIP] },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(<AppStateProvider><ClipReviewPage /></AppStateProvider>);

    const player = await waitFor(() => {
      const element = document.querySelector("video");
      if (!element) throw new Error("video not loaded");
      return element;
    });
    fireEvent.error(player);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("videos/download-url"))).toHaveLength(2));
    fireEvent.error(player);
    expect((await screen.findByRole("alert")).textContent).toMatch(/Playback failed/);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("videos/download-url"))).toHaveLength(2);
  });

  test("clicking a player box prefills the corrections panel; field view is calibration-gated", async () => {
    const payload = {
      clip_id: "c-1",
      tracklets: [
        {
          id: "t-1",
          player_id: null,
          start_frame: 0,
          end_frame: 300,
          track_confidence: 0.9,
          team_label: "home",
          position_group: "Skill",
          side_of_ball: "offense",
          track_points: [
            {
              frame_number: 0,
              field_x: 30,
              field_y: 5,
              bbox: [100, 200, 160, 320],
              detection_confidence: 0.9,
            },
          ],
        },
      ],
      events: [],
      labels: [],
      metrics: [],
      layers_available: { tracklets: true, events: false, labels: false, metrics: false },
      calibration: {
        analytics_safe: false,
        reason: "Field lines were not detected in this footage.",
        reason_codes: ["no_lines"],
        confidence: 0.2,
      },
    };
    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: payload },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1/clips", body: [SAMPLE_CLIP] },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("overlay-tracklet-t-1")).toBeTruthy();
    });

    // Clicking the box opens Corrections prefilled with that track selected.
    fireEvent.click(screen.getByTestId("overlay-tracklet-t-1"));
    await waitFor(() => {
      expect(screen.getByTestId("correction-player-select")).toBeTruthy();
    });
    const trackSelect = document.getElementById("correction-tracklet") as HTMLSelectElement;
    expect(trackSelect.value).toBe("t-1");
    fireEvent.change(screen.getByLabelText("Correct team"), { target: { value: "home" } });
    fireEvent.click(screen.getByRole("tab", { name: "Details" }));
    fireEvent.click(screen.getByRole("tab", { name: "Tag" }));
    expect((screen.getByLabelText("Correct team") as HTMLSelectElement).value).toBe("home");

    // Field view is gated: uncalibrated footage explains itself instead of
    // plotting untrustworthy positions.
    await waitFor(() => {
      expect(screen.getByTestId("field-minimap-gated")).toBeTruthy();
    });
    expect(screen.getByTestId("video-stage").contains(screen.getByTestId("field-inset"))).toBe(true);
    fireEvent.click(screen.getByTestId("overlay-toggle-field"));
    expect(screen.queryByTestId("field-inset")).toBeNull();
    // Reason text shows in both the calibration banner and the gated panel.
    expect(screen.getAllByText(/Field lines were not detected/).length).toBeGreaterThan(0);
    expect(screen.queryByTestId("field-diagram")).toBeNull();
  });

  test("renders overlay error state when overlay endpoint fails", async () => {
    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: { detail: "boom" }, ok: false, status: 500 },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("overlay-error")).toBeTruthy();
    });
  });

  test("shows a dismissable calibration banner + gated spatial metrics when analytics_safe is false", async () => {
    const payload = {
      ...EMPTY_OVERLAYS,
      capture_regime: "unconstrained",
      layers_available: {
        tracklets: true,
        events: false,
        labels: false,
        metrics: false,
      },
      tracklets: [
        {
          id: "t-1",
          player_id: null,
          start_frame: 0,
          end_frame: 30,
          track_confidence: 0.9,
          team_label: "home",
          position_group: null,
          side_of_ball: null,
          track_points: [],
        },
      ],
      calibration: {
        analytics_safe: false,
        reason:
          "Not enough yard lines were visible to map the field, so spatial metrics are hidden for this footage.",
        reason_codes: ["insufficient_yard_lines"],
        confidence: 0.31,
      },
    };

    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: payload },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    const { container } = render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("calibration-banner")).toBeTruthy();
    });
    expect(screen.getByTestId("calibration-banner").textContent).toContain(
      "Not enough yard lines were visible",
    );
    // Non-blocking: the video player still renders alongside the banner.
    expect(container.querySelector("video")).toBeTruthy();
    // Overlay summary carries the gated Spatial Metrics card with the same
    // coach-readable reason.
    expect(screen.getByTestId("card-gated-reason").textContent).toContain(
      "Not enough yard lines were visible",
    );

    // Dismiss hides the banner but nothing else.
    fireEvent.click(screen.getByTestId("calibration-banner-dismiss"));
    await waitFor(() => {
      expect(screen.queryByTestId("calibration-banner")).toBeNull();
    });
    expect(container.querySelector("video")).toBeTruthy();
  });

  test("shows no calibration banner when analytics_safe is true", async () => {
    const payload = {
      ...EMPTY_OVERLAYS,
      capture_regime: "drone_follow",
      calibration: {
        analytics_safe: true,
        reason: null,
        reason_codes: [],
        confidence: 0.94,
      },
    };

    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: payload },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("overlay-empty")).toBeTruthy();
    });
    expect(screen.queryByTestId("calibration-banner")).toBeNull();
    expect(screen.queryByTestId("card-gated-reason")).toBeNull();
  });

  test("renders degraded summary when only some overlay layers have data", async () => {
    const payload = {
      clip_id: "c-1",
      tracklets: [
        {
          id: "t-1",
          player_id: null,
          start_frame: 0,
          end_frame: 30,
          track_confidence: 0.9,
          team_label: "home",
          position_group: null,
          side_of_ball: null,
          track_points: [],
        },
      ],
      events: [],
      labels: [],
      metrics: [],
      layers_available: { tracklets: true, events: false, labels: false, metrics: false },
    };

    installFetchRoutes([
      { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/x" } },
      { url: "/api/v1/clips/c-1/overlays", body: payload },
      { url: "/api/v1/clips/c-1", body: SAMPLE_CLIP },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
      { url: "/api/v1/self-scout/tendencies", body: { pre_snap_tells: [] } },
    ]);

    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("overlay-degraded")).toBeTruthy();
    });
    expect(
      (screen.getByTestId("overlay-toggle-events") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByTestId("overlay-toggle-tracks") as HTMLButtonElement).disabled,
    ).toBe(false);
  });
});

describe("ClipReviewPage playback source", () => {
  function routes(clip: Record<string, unknown>) {
    return installFetchRoutes([
      { url: /\/api\/v1\/videos\/download-url\?.*bucket=clips(?:&|$)/, body: { downloadUrl: "https://dl.test/clips" } },
      { url: /\/api\/v1\/videos\/download-url\?.*bucket=raw(?:&|$)/, body: { downloadUrl: "https://dl.test/raw" } },
      { url: "/api/v1/clips/c-1/overlays", body: EMPTY_OVERLAYS },
      { url: "/api/v1/clips/c-1", body: clip },
      { url: "/api/v1/videos/v-1/clips", body: [clip] },
      { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
      { url: "/api/v1/players", body: [] },
      { url: "/api/v1/inbox/status", body: [] },
      { url: "/api/v1/videos", body: [] },
      { url: "/api/v1/jobs", body: [] },
    ]);
  }

  function requestedBuckets(fetchMock: ReturnType<typeof vi.fn>): string[] {
    return fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes("videos/download-url"))
      .map((url) => new URL(url).searchParams.get("bucket") ?? "");
  }

  test("plays the clean per-play proxy instead of the full recording", async () => {
    const fetchMock = routes({ ...SAMPLE_CLIP, provenance: { playback: { kind: "clean_clip" } } });
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );
    await screen.findByLabelText("Clip 7 video");
    expect(requestedBuckets(fetchMock)[0]).toBe("clips");
  });

  test("falls back to the full recording when the proxy fails to play", async () => {
    const fetchMock = routes({ ...SAMPLE_CLIP, provenance: { playback: { kind: "clean_clip" } } });
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );
    fireEvent.error(await screen.findByLabelText("Clip 7 video"));
    await waitFor(() => expect(requestedBuckets(fetchMock)).toEqual(["clips", "raw"]));
  });

  test("converts the saved clip-local playhead when falling back to the recording", async () => {
    const fetchMock = routes({ ...SAMPLE_CLIP, provenance: { playback: { kind: "clean_clip" } } });
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(<AppStateProvider><ClipReviewPage /></AppStateProvider>);
    const player = await screen.findByLabelText("Clip 7 video") as HTMLVideoElement;
    fireEvent.loadedMetadata(player);
    player.currentTime = 2;
    fireEvent.error(player);
    await waitFor(() => expect(requestedBuckets(fetchMock)).toEqual(["clips", "raw"]));
    await waitFor(() => expect(player.getAttribute("src")).toBe("https://dl.test/raw"));
    fireEvent.loadedMetadata(player);
    expect(player.currentTime).toBe(12);
  });

  test("converts the saved recording playhead when falling back to the clip", async () => {
    const fetchMock = routes(SAMPLE_CLIP);
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(<AppStateProvider><ClipReviewPage /></AppStateProvider>);
    const player = await screen.findByLabelText("Clip 7 video") as HTMLVideoElement;
    fireEvent.loadedMetadata(player);
    player.currentTime = 12;
    fireEvent.error(player);
    await waitFor(() => expect(requestedBuckets(fetchMock)).toEqual(["raw", "clips"]));
    await waitFor(() => expect(player.getAttribute("src")).toBe("https://dl.test/clips"));
    fireEvent.loadedMetadata(player);
    expect(player.currentTime).toBe(2);
  });

  test("keeps the full recording for rendered previews with burned-in overlays", async () => {
    const fetchMock = routes(SAMPLE_CLIP);
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );
    await screen.findByLabelText("Clip 7 video");
    expect(requestedBuckets(fetchMock)[0]).toBe("raw");
  });
});


test("retired boundary deep links stay watchable and expose the current recording without edit forms", async () => {
  window.history.replaceState({}, "", "/clip-review/?clipId=c-1&position=WR&from=%2Fteam-stats%2F");
  installFetchRoutes([
    { url: "/api/v1/videos/download-url", body: { downloadUrl: "https://dl.test/proxy" } },
    { url: "/api/v1/clips/c-1/overlays", body: EMPTY_OVERLAYS },
    { url: "/api/v1/clips/c-1", body: { ...SAMPLE_CLIP, provenance: { state: "superseded", replacement_clip_ids: ["c-2"] } } },
    { url: "/api/v1/videos/v-1/clips", body: [{ ...SAMPLE_CLIP, id: "c-2" }] },
    { url: "/api/v1/videos/v-1", body: SAMPLE_VIDEO },
    { url: "/api/v1/videos", body: [] }, { url: "/api/v1/jobs", body: [] }, { url: "/api/v1/players", body: [] }, { url: "/api/v1/inbox/status", body: [] },
  ]);
  const { ClipReviewPage, AppStateProvider } = await importPage();
  render(<AppStateProvider><ClipReviewPage /></AppStateProvider>);
  await screen.findByTestId("video-transport");
  const link = screen.getByRole("link", { name: "Open current recording" });
  const url = new URL(link.getAttribute("href")!, "https://film.test");
  expect(url.searchParams.get("position")).toBe("WR");
  expect(url.searchParams.get("videoId")).toBe("v-1");
  expect(url.searchParams.has("clipId")).toBe(false);
  fireEvent.click(screen.getByRole("tab", { name: /Tag/ }));
  expect(screen.getByText(/preserved version is read-only/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Save correction" })).toBeNull();
});
