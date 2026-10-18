import { afterEach, describe, expect, test, vi } from "vitest";

vi.mock("./endpoints", () => ({ apiBase: () => "https://api.test" }));

import { uploadFilmMultipart } from "./multipart-upload";

type Call = { url: string; method: string; auth?: string };

function file(bytes: number): File {
  return new File([new Uint8Array(bytes)], "game.mp4", { type: "video/mp4" });
}

function server(handlers: { part?: (n: number, attempt: number) => Response; create?: () => Response }) {
  const calls: Call[] = [];
  const attempts = new Map<number, number>();
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    const method = init.method ?? "GET";
    const auth = (init.headers as Record<string, string> | undefined)?.Authorization;
    calls.push({ url, method, auth });
    if (url.endsWith("/create")) return handlers.create?.() ?? Response.json({ key: "raw/k", uploadId: "u1" });
    if (url.includes("/part?")) {
      const n = Number(new URL(url).searchParams.get("partNumber"));
      const attempt = (attempts.get(n) ?? 0) + 1;
      attempts.set(n, attempt);
      return handlers.part?.(n, attempt) ?? Response.json({ partNumber: n, etag: `e${n}` });
    }
    if (url.endsWith("/complete")) {
      const body = JSON.parse(String(init.body)) as { parts: { partNumber: number }[] };
      return Response.json({ key: "raw/k", size: 25, etag: "x", storageUri: "s3://raw-video/raw/k", parts: body.parts });
    }
    if (url.includes("/abort?")) return new Response(null, { status: 204 });
    return new Response("not found", { status: 404 });
  }));
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("uploadFilmMultipart", () => {
  test("uploads every part with a fresh token and completes in order", async () => {
    const calls = server({});
    let token = 0;
    const progress: number[] = [];
    const result = await uploadFilmMultipart(file(25), {
      getToken: () => `t${++token}`,
      partSize: 10,
      onProgress: (loaded) => progress.push(loaded),
    });
    expect(result?.storageUri).toBe("s3://raw-video/raw/k");
    const parts = calls.filter((c) => c.url.includes("/part?"));
    expect(parts).toHaveLength(3);
    // Every request re-reads the token (long uploads outlive one token).
    expect(new Set(calls.map((c) => c.auth)).size).toBe(calls.length);
    expect(progress.at(-1)).toBe(25);
  });

  test("retries a part that failed on the server", async () => {
    const calls = server({
      part: (n, attempt) =>
        n === 2 && attempt === 1
          ? new Response("busy", { status: 503 })
          : Response.json({ partNumber: n, etag: `e${n}` }),
    });
    const result = await uploadFilmMultipart(file(25), { getToken: () => "t", partSize: 10, backoffMs: () => 0 });
    expect(result).not.toBeNull();
    expect(calls.filter((c) => c.url.includes("partNumber=2"))).toHaveLength(2);
  });

  test("returns null when the server has no multipart route", async () => {
    server({ create: () => new Response("nope", { status: 404 }) });
    expect(await uploadFilmMultipart(file(25), { getToken: () => "t", partSize: 10 })).toBeNull();
  });

  test("aborts the upload and reports a plain error when a part keeps failing", async () => {
    const calls = server({ part: () => new Response("{\"detail\":\"Invalid storage key\"}", { status: 400 }) });
    await expect(
      uploadFilmMultipart(file(25), { getToken: () => "t", partSize: 10, backoffMs: () => 0 }),
    ).rejects.toMatchObject({ name: "ApiError", status: 400 });
    await vi.waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
  });
});
