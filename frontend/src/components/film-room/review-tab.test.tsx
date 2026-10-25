import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ApiClip, ApiVideo } from "@/lib/types";

const context = vi.hoisted(() => ({
  authToken: "protocol-token",
  data: { videos: [{ id: "shared-only-one" }] },
  sessionType: "game", selectedDate: "2026-01-01",
}));
vi.mock("next/link", () => ({ default: ({ children, ...rest }: React.ComponentProps<"a">) => <a {...rest}>{children}</a> }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => context }));

import { ReviewTab } from "./review-tab";

function video(id: string, session = "practice-session"): ApiVideo {
  return { id, filename: `${id}.mp4`, status: "ready", practice_session_id: session,
    created_at: "2026-04-16T12:00:00Z", session_kind: "practice" } as ApiVideo;
}
function clip(id: string): ApiClip {
  return { id: `clip-${id}`, video_id: id, start_time: 0, end_time: 7, play_number: 1,
    is_reviewed: false, created_at: "2026-04-16T12:00:00Z" } as ApiClip;
}
function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body,
    text: async () => JSON.stringify(body) } as Response;
}
function serve(catalog: (url: URL) => unknown | Promise<unknown>) {
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/clips")) return response([clip(url.pathname.split("/").at(-2)!)]);
    if (url.pathname === "/api/v1/videos") return response(await catalog(url));
    throw new Error(`Unexpected protocol request ${url.pathname}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => { vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test"); });
afterEach(() => { cleanup(); context.authToken = "protocol-token"; vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe("Film Room complete recording picker", () => {
  test("shows all 30 API recordings despite the shared list's saved date/type filters", async () => {
    const fetch = serve(() => Array.from({ length: 30 }, (_, index) => video(`drone-${index}`)));
    render(<ReviewTab />);
    await waitFor(() => expect(screen.getByTestId("review-video-picker").querySelectorAll("option")).toHaveLength(30));
    expect(screen.getByTestId("recording-scope").textContent).toContain("30 recordings · All uploaded recordings");
    expect(screen.getByRole("link", { name: "Add recordings" }).getAttribute("href")).toBe("/film-room/?tab=upload");
    const requests = fetch.mock.calls.map(([url]) => new URL(String(url))).filter((url) => url.pathname === "/api/v1/videos");
    expect(requests).toHaveLength(1);
    expect([...requests[0].searchParams.keys()].sort()).toEqual(["limit", "offset"]);
    fireEvent.change(screen.getByTestId("review-video-picker"), { target: { value: "drone-29" } });
    await screen.findByTestId("review-clip-clip-drone-29");
  });

  test("loads and selects a recording beyond the first 200", async () => {
    serve((url) => url.searchParams.get("offset") === "0"
      ? Array.from({ length: 200 }, (_, index) => video(`drone-${index}`)) : [video("drone-200")]);
    render(<ReviewTab />);
    await waitFor(() => expect(screen.getByTestId("review-video-picker").querySelectorAll("option")).toHaveLength(201));
    fireEvent.change(screen.getByTestId("review-video-picker"), { target: { value: "drone-200" } });
    await screen.findByTestId("review-clip-clip-drone-200");
  });

  test("makes a one-recording session scope visible and offers all recordings", async () => {
    const fetch = serve((url) => url.searchParams.has("practice_session_id") ? [video("drone-0")] : [video("drone-0"), video("drone-1")]);
    const onSessionChange = vi.fn();
    const view = render(<ReviewTab sessionId="practice-session" onSessionChange={onSessionChange} />);
    await waitFor(() => expect(screen.getByTestId("recording-scope").textContent).toContain("1 recording in the selected session"));
    expect(screen.getByRole("link", { name: "Add recordings" }).getAttribute("href")).toBe("/film-room/?tab=browse&sessionId=practice-session");
    fireEvent.click(screen.getByRole("button", { name: "Show all recordings" }));
    expect(onSessionChange).toHaveBeenCalledWith("");
    view.rerender(<ReviewTab onSessionChange={onSessionChange} />);
    await waitFor(() => expect(screen.getByTestId("recording-scope").textContent).toContain("2 recordings · All uploaded recordings"));
    expect(fetch.mock.calls.some(([input]) => new URL(String(input)).searchParams.get("practice_session_id") === "practice-session")).toBe(true);
  });

  test("follows new explicit video links without rendering the previous video's clips", async () => {
    const later = deferred<Response>();
    const fetch = serve(() => [video("first"), video("second")]);
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (input) => String(input).includes("/second/clips") ? later.promise : original(input));
    const view = render(<ReviewTab initialVideoId="first" />);
    await screen.findByTestId("review-clip-clip-first");
    view.rerender(<ReviewTab initialVideoId="second" />);
    expect(screen.queryByTestId("review-clip-clip-first")).toBeNull();
    later.resolve(response([clip("second")]));
    await screen.findByTestId("review-clip-clip-second");
    expect((screen.getByTestId("review-video-picker") as HTMLSelectElement).value).toBe("second");
  });

  test("ignores a late old-scope catalog and clears old selections for an empty scope", async () => {
    const old = deferred<ApiVideo[]>();
    serve((url) => url.searchParams.get("practice_session_id") === "old" ? old.promise : []);
    const view = render(<ReviewTab sessionId="old" initialVideoId="old-video" />);
    view.rerender(<ReviewTab sessionId="new" />);
    await screen.findByText("No recordings in the selected session. Show all recordings or add film to this session.");
    await act(async () => { old.resolve([video("old-video", "old")]); await old.promise; });
    await waitFor(() => expect(screen.getByTestId("recording-scope").textContent).toContain("0 recordings"));
    expect(screen.queryByTestId("review-clip-clip-old-video")).toBeNull();
    expect((screen.getByTestId("review-video-picker") as HTMLSelectElement).value).toBe("");
  });

  test("Back to an explicit video does not resurrect a manual choice from its earlier visit", async () => {
    serve(() => [video("first"), video("second"), video("third")]);
    const view = render(<ReviewTab initialVideoId="first" />);
    await screen.findByTestId("review-clip-clip-first");
    fireEvent.change(screen.getByTestId("review-video-picker"), { target: { value: "second" } });
    await screen.findByTestId("review-clip-clip-second");
    view.rerender(<ReviewTab initialVideoId="third" />);
    await screen.findByTestId("review-clip-clip-third");
    view.rerender(<ReviewTab initialVideoId="first" />);
    await screen.findByTestId("review-clip-clip-first");
    expect((screen.getByTestId("review-video-picker") as HTMLSelectElement).value).toBe("first");
  });

  test("does not silently replace an unavailable explicit recording", async () => {
    const fetch = serve(() => [video("available")]);
    render(<ReviewTab initialVideoId="missing" />);
    await screen.findByText("The requested recording is not available in this library. Choose a recording above, or add it to the library.");
    expect(fetch.mock.calls.some(([input]) => String(input).includes("/clips"))).toBe(false);
    fireEvent.change(screen.getByTestId("review-video-picker"), { target: { value: "available" } });
    await screen.findByTestId("review-clip-clip-available");
  });

  test("shows catalog failure and retries the recording list", async () => {
    let fail = true;
    const fetch = serve(() => { if (fail) throw new TypeError("Protocol network failure"); return [video("recovered")]; });
    render(<ReviewTab />);
    await screen.findByRole("alert");
    expect(screen.getByTestId("recording-scope").textContent).toContain("Recording list unavailable");
    expect(fetch.mock.calls.some(([input]) => String(input).includes("/clips"))).toBe(false);
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry recordings" }));
    await screen.findByTestId("review-clip-clip-recovered");
  });

  test("refresh keeps the valid default recording when a newer recording arrives", async () => {
    let catalog = [video("original")];
    serve(() => catalog);
    render(<ReviewTab />);
    await screen.findByTestId("review-clip-clip-original");
    catalog = [{ ...video("newer"), created_at: "2026-04-17T12:00:00Z" }, video("original")];
    fireEvent.click(screen.getByRole("button", { name: "Refresh recordings" }));
    await waitFor(() => expect(screen.getByTestId("recording-scope").textContent).toContain("2 recordings"));
    expect((screen.getByTestId("review-video-picker") as HTMLSelectElement).value).toBe("original");
    await screen.findByTestId("review-clip-clip-original");
  });

  test.each(["empty", "error"])("changing a loaded scope to an %s scope never retains old clips", async (mode) => {
    const fetch = serve((url) => {
      if (url.searchParams.get("practice_session_id") === "old") return [video("old-video", "old")];
      if (mode === "error") throw new TypeError("Protocol failed new scope");
      return [];
    });
    const view = render(<ReviewTab sessionId="old" />);
    await screen.findByTestId("review-clip-clip-old-video");
    const priorRequests = fetch.mock.calls.filter(([input]) => String(input).includes("/old-video/clips")).length;
    view.rerender(<ReviewTab sessionId="new" />);
    expect(screen.queryByTestId("review-clip-clip-old-video")).toBeNull();
    if (mode === "error") await screen.findByRole("alert");
    else await screen.findByText("No recordings in the selected session. Show all recordings or add film to this session.");
    expect(fetch.mock.calls.filter(([input]) => String(input).includes("/old-video/clips"))).toHaveLength(priorRequests);
  });

  test("late old-recording clips cannot replace the new recording's clips", async () => {
    const older = deferred<Response>();
    const fetch = serve(() => [video("first"), video("second")]);
    const original = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (input) => String(input).includes("/first/clips") ? older.promise : original(input));
    const view = render(<ReviewTab initialVideoId="first" />);
    await waitFor(() => expect(fetch.mock.calls.some(([input]) => String(input).includes("/first/clips"))).toBe(true));
    view.rerender(<ReviewTab initialVideoId="second" />);
    await screen.findByTestId("review-clip-clip-second");
    await act(async () => { older.resolve(response([clip("first")])); await older.promise; });
    expect(screen.queryByTestId("review-clip-clip-first")).toBeNull();
    expect(screen.getByTestId("review-clip-clip-second")).toBeTruthy();
  });

  test("a late catalog from the previous authenticated context is ignored", async () => {
    const older = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/clips")) return response([clip("new-video")]);
      if ((init?.headers as Record<string, string>).Authorization === "Bearer protocol-token") return older.promise;
      return response([video("new-video")]);
    }));
    const view = render(<ReviewTab />);
    context.authToken = "other-protocol-token";
    view.rerender(<ReviewTab />);
    await screen.findByTestId("review-clip-clip-new-video");
    await act(async () => { older.resolve(response([video("old-video")])); await older.promise; });
    expect((screen.getByTestId("review-video-picker") as HTMLSelectElement).value).toBe("new-video");
    expect(screen.queryByText(/old-video.mp4/)).toBeNull();
  });
});
