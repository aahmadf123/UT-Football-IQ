import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchForRead } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("fetchForRead", () => {
  it("retries a read once after a server error", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("{}", { status: 500 }))
      .mockResolvedValueOnce(new Response("[]", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const pending = fetchForRead("https://api.test/api/v1/players");
    await vi.runAllTimersAsync();
    expect((await pending).status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("returns the second failure instead of retrying forever", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    const pending = fetchForRead("https://api.test/api/v1/players");
    await vi.runAllTimersAsync();
    expect((await pending).status).toBe(503);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry client errors or network failures", async () => {
    const notFound = vi.fn().mockResolvedValue(new Response("{}", { status: 404 }));
    vi.stubGlobal("fetch", notFound);
    expect((await fetchForRead("https://api.test/x")).status).toBe(404);
    expect(notFound).toHaveBeenCalledOnce();

    const offline = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", offline);
    await expect(fetchForRead("https://api.test/x")).rejects.toThrow("Failed to fetch");
    expect(offline).toHaveBeenCalledOnce();
  });
});
