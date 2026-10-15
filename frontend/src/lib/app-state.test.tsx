import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, render, waitFor } from "@testing-library/react";

// We capture the context value via a probe component so each test can read
// the live app-state snapshot without depending on UI structure.
type Snapshot = ReturnType<typeof import("./app-state").useAppState>;

function makeProbe() {
  const captured: { current: Snapshot | null } = { current: null };
  function Probe(props: { useHook: () => Snapshot }) {
    captured.current = props.useHook();
    return null;
  }
  return { captured, Probe };
}

async function freshImport() {
  // Reset module cache so app-state re-reads useMocks() on import. This is
  // required because useState's initial value is evaluated once per mount,
  // and React StrictMode is not used here so a single render captures the
  // value driven by the current env vars.
  vi.resetModules();
  const mod = await import("./app-state");
  return mod;
}

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
  vi.useRealTimers();
  window.localStorage.clear();
});

describe("AppStateProvider — local API unavailable", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
  });

  test("empty data + offline status when neither mocks nor API are configured", async () => {
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();

    render(
      <AppStateProvider>
        <Probe useHook={useAppState} />
      </AppStateProvider>,
    );

    // The fetch effect runs on mount and synchronously sets "offline" because
    // NEXT_PUBLIC_API_URL is empty. We still await one tick for the effect.
    await waitFor(() => {
      expect(captured.current?.apiStatus).toBe("offline");
    });

    const state = captured.current!;
    expect(state.data.players.length).toBe(0);
    expect(state.data.videos.length).toBe(0);
    expect(state.data.jobs.length).toBe(0);
    expect(state.mockMode).toBe(false);
  });

  test("a disconnected import fails without pretending the browser saved a recording", async () => {
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();
    render(<AppStateProvider><Probe useHook={useAppState} /></AppStateProvider>);
    await act(async () => { await captured.current?.addUploads([new File(["video"], "practice.mp4", { type: "video/mp4" })]); });
    await waitFor(() => expect(captured.current?.uploads[0].phase).toBe("error"));
    expect(captured.current?.data.videos).toEqual([]);
    expect(captured.current?.uploads.some((upload) => upload.phase === "done")).toBe(false);
    expect(window.localStorage.getItem("football_iq_app_state_v1")).not.toContain("practice.mp4");
  });
});

describe("AppStateProvider — mock mode (NEXT_PUBLIC_USE_MOCKS=1)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "1");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
  });

  test("mock arrays populated and apiStatus is 'mock'", async () => {
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();

    render(
      <AppStateProvider>
        <Probe useHook={useAppState} />
      </AppStateProvider>,
    );

    await waitFor(() => {
      expect(captured.current?.apiStatus).toBe("mock");
    });

    const state = captured.current!;
    expect(state.data.videos.length).toBeGreaterThan(0);
    expect(state.data.players.length).toBeGreaterThan(0);
    expect(state.data.jobs.length).toBeGreaterThan(0);
    expect(state.mockMode).toBe(true);
  });
});

describe("AppStateProvider — empty live response does not fall back to mock", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", "");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "http://api.test");
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
    }));
    vi.stubGlobal("fetch", fetchMock);
  });

  test("empty arrays from the API stay empty (status: live, no mock substitution)", async () => {
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();

    await act(async () => {
      render(
        <AppStateProvider>
          <Probe useHook={useAppState} />
        </AppStateProvider>,
      );
    });

    await waitFor(() => {
      expect(captured.current?.apiStatus).toBe("live");
    });

    const state = captured.current!;
    expect(state.data.players.length).toBe(0);
    expect(state.data.videos.length).toBe(0);
    expect(state.data.jobs.length).toBe(0);
    expect(state.mockMode).toBe(false);
  });

  test("old browser upload names cannot reconstitute film that was never saved", async () => {
    window.localStorage.setItem("football_iq_app_state_v1", JSON.stringify({ sessionType: "all", selectedDate: "", uploadedNames: ["lost-practice.mp4"] }));
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();
    render(<AppStateProvider><Probe useHook={useAppState} /></AppStateProvider>);
    await waitFor(() => expect(captured.current?.apiStatus).toBe("live"));
    expect(captured.current?.uploads).toEqual([]);
    expect(captured.current?.data.videos).toEqual([]);
    expect(window.localStorage.getItem("football_iq_app_state_v1")).not.toContain("lost-practice.mp4");
  });

  test("polling moves the shared film and jobs into their completed state", async () => {
    vi.useFakeTimers();
    let ready = false;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: true, status: 200, json: async () => url.includes("/api/v1/videos") ? [{ id: "v1", filename: "practice.mp4", status: ready ? "ready" : "uploaded", created_at: "2026-10-14", recorded_at: "2026-10-12" }] : url.includes("/api/v1/jobs") ? [{ id: "j1", status: ready ? "succeeded" : "running" }] : [] })));
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();
    await act(async () => { render(<AppStateProvider><Probe useHook={useAppState} /></AppStateProvider>); });
    expect(captured.current?.data.videos[0].status).toBe("uploaded");
    ready = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(captured.current?.data.videos[0].status).toBe("ready");
    expect(captured.current?.data.jobs[0].status).toBe("succeeded");
    expect(captured.current?.availableDates).toContain("2026-10-12");
    expect(captured.current?.availableDates).not.toContain("2026-10-14");
  });

  test("does not send recorded_at filters until a date is selected", async () => {
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();

    await act(async () => {
      render(
        <AppStateProvider>
          <Probe useHook={useAppState} />
        </AppStateProvider>,
      );
    });

    await waitFor(() => {
      expect(captured.current?.apiStatus).toBe("live");
    });

    const fetchMock = vi.mocked(fetch);
    const videosCall = fetchMock.mock.calls
      .map((call) => String(call[0]))
      .find((url) => url.includes("/api/v1/videos"));
    expect(videosCall).toBe("http://api.test/api/v1/videos");
  });

  test("sends full-day recorded_at range when a date is selected", async () => {
    const { AppStateProvider, useAppState } = await freshImport();
    const { captured, Probe } = makeProbe();

    await act(async () => {
      render(
        <AppStateProvider>
          <Probe useHook={useAppState} />
        </AppStateProvider>,
      );
    });
    await waitFor(() => {
      expect(captured.current?.apiStatus).toBe("live");
    });

    await act(async () => {
      captured.current?.setSelectedDate("2026-05-20");
    });

    await waitFor(() => {
      const lastVideosCall = vi
        .mocked(fetch)
        .mock.calls.map((call) => String(call[0]))
        .filter((url) => url.includes("/api/v1/videos"))
        .at(-1);
      expect(lastVideosCall).toContain("recorded_after=2026-05-20T00%3A00%3A00Z");
      expect(lastVideosCall).toContain("recorded_before=2026-05-20T23%3A59%3A59.999999Z");
    });
  });
});
