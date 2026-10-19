import { afterEach, describe, expect, it, vi } from "vitest";
import { RETRY_DELAYS_MS, containerUnavailable, proxyToBackend } from "../src/proxy";

function env(...outcomes: Array<Response | Error>) {
  const fetch = vi.fn();
  for (const outcome of outcomes) {
    if (outcome instanceof Error) fetch.mockRejectedValueOnce(outcome);
    else fetch.mockResolvedValueOnce(outcome);
  }
  return { env: { BACKEND: { getByName: () => ({ fetch }) } } as never, fetch };
}

const starting = () => new Response("Failed to start container: booting", { status: 500 });

describe("proxy during container restarts", () => {
  afterEach(() => vi.useRealTimers());

  it("retries a read until the new container answers", async () => {
    vi.useFakeTimers();
    const failedFetch = vi.fn().mockRejectedValue(new Error("Durable Object reset"));
    const recoveredFetch = vi.fn().mockResolvedValue(Response.json({ ok: true }));
    const getByName = vi.fn()
      .mockReturnValueOnce({ fetch: failedFetch })
      .mockReturnValue({ fetch: recoveredFetch });
    const e = { BACKEND: { getByName } } as never;
    const pending = proxyToBackend(new Request("https://edge.test/api/v1/players"), e);
    await vi.runAllTimersAsync();
    const response = await pending;
    expect(response.status).toBe(200);
    expect(failedFetch).toHaveBeenCalledOnce();
    expect(recoveredFetch).toHaveBeenCalledOnce();
    expect(getByName).toHaveBeenCalledTimes(2);
  });

  it("gives up with a friendly retryable 503", async () => {
    vi.useFakeTimers();
    const { env: e, fetch } = env(...Array.from({ length: RETRY_DELAYS_MS.length + 1 }, starting));
    const pending = proxyToBackend(new Request("https://edge.test/api/v1/players"), e);
    await vi.runAllTimersAsync();
    const response = await pending;
    expect(fetch).toHaveBeenCalledTimes(RETRY_DELAYS_MS.length + 1);
    expect(response.status).toBe(503);
    expect(response.headers.get("Retry-After")).toBe("5");
    expect(await response.json()).toEqual({ detail: "The server is restarting. Try again in a moment." });
  });

  it("never replays a write", async () => {
    const { env: e, fetch } = env(new Response("Container suddenly disconnected, try again", { status: 500 }));
    const request = new Request("https://edge.test/api/v1/clips", { method: "POST", body: "{}" });
    const response = await proxyToBackend(request, e);
    expect(fetch).toHaveBeenCalledOnce();
    expect(response.status).toBe(503);
  });

  it("passes the backend's own errors through", async () => {
    const { env: e, fetch } = env(new Response("Internal Server Error", { status: 500 }));
    const response = await proxyToBackend(new Request("https://edge.test/api/v1/players"), e);
    expect(fetch).toHaveBeenCalledOnce();
    expect(response.status).toBe(500);
    expect(await response.text()).toBe("Internal Server Error");
  });

  it("recognises only the containers library's failure text", async () => {
    expect(await containerUnavailable(Response.json({ detail: "x" }, { status: 503 }))).toBeNull();
    expect(await containerUnavailable(new Response("There is no Container instance available", { status: 503 })))
      .toContain("503");
  });
});
