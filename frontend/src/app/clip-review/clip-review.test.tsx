import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock("next/image", () => ({
  default: () => null,
}));

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  usePathname: () => "/clip-review",
  useSearchParams: () => new URLSearchParams("clipId=c-1"),
  useRouter: () => ({ push: routerPush, replace: vi.fn(), back: vi.fn() }),
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
  test("shows offline notice when NEXT_PUBLIC_API_URL is missing", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    const { ClipReviewPage, AppStateProvider } = await importPage();
    render(
      <AppStateProvider>
        <ClipReviewPage />
      </AppStateProvider>,
    );
    await waitFor(() => {
      expect(screen.getByText(/Clip Review is unavailable offline/i)).toBeTruthy();
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
        },
      ],
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
    expect(screen.getByText(/yards_gained/)).toBeTruthy();

    // Toggling "raw" hides the overlay SVG.
    fireEvent.click(screen.getByTestId("overlay-toggle-raw"));
    await waitFor(() => {
      expect(screen.queryByTestId("overlay-tracklet-t-1")).toBeNull();
    });
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

    // Clicking an event marker seeks the (clip-asset) video to the event time.
    fireEvent.click(screen.getByTestId("event-marker-e-1"));
    expect(video.currentTime).toBeCloseTo(0.5, 3);
    fireEvent.timeUpdate(video);
    expect(screen.getByTestId("transport-readout").textContent).toContain("0.50s");

    // Frame stepping moves one frame at the video's own fps (30).
    fireEvent.click(screen.getByTestId("transport-frame-forward"));
    expect(video.currentTime).toBeCloseTo((16 + 0.5) / 30, 3);

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

    // Field view is gated: uncalibrated footage explains itself instead of
    // plotting untrustworthy positions.
    fireEvent.click(screen.getByTestId("overlay-toggle-field"));
    await waitFor(() => {
      expect(screen.getByTestId("field-minimap-gated")).toBeTruthy();
    });
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
