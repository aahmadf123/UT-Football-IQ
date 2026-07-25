import { env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import type { Env } from "../src/env";
import { handlePreflight, resolveOrigin, withCors } from "../src/cors";

const testEnv = env as Env;

function req(origin?: string, init?: RequestInit): Request {
  return new Request("https://edge.test/api/v1/videos", {
    ...init,
    headers: { ...(origin ? { Origin: origin } : {}), ...(init?.headers ?? {}) },
  });
}

describe("resolveOrigin", () => {
  it("allows an exact allowlisted origin", () => {
    expect(resolveOrigin(req("https://app.example.com"), testEnv)).toBe("https://app.example.com");
    expect(resolveOrigin(req("http://localhost:3000"), testEnv)).toBe("http://localhost:3000");
  });

  it("allows an origin matching the preview regex", () => {
    expect(resolveOrigin(req("https://footiq-abc123.vercel.app"), testEnv)).toBe(
      "https://footiq-abc123.vercel.app",
    );
  });

  it("anchors the regex so a lookalike origin cannot slip through", () => {
    // Unanchored, `https://footiq-[a-z0-9-]+\.vercel\.app` would match inside
    // this string and hand an attacker credentialed CORS.
    expect(resolveOrigin(req("https://evil.com/?x=https://footiq-a.vercel.app"), testEnv)).toBeNull();
    expect(resolveOrigin(req("https://footiq-a.vercel.app.evil.com"), testEnv)).toBeNull();
  });

  it("denies an unknown origin", () => {
    expect(resolveOrigin(req("https://evil.com"), testEnv)).toBeNull();
  });

  it("returns null when there is no Origin header", () => {
    // Server-to-server callers (the GPU worker) send no Origin and need none.
    expect(resolveOrigin(req(), testEnv)).toBeNull();
  });
});

describe("withCors", () => {
  it("echoes an allowed origin and varies on it", () => {
    const response = withCors(new Response("ok"), req("https://app.example.com"), testEnv);
    expect(response.headers.get("Access-Control-Allow-Origin")).toBe("https://app.example.com");
    expect(response.headers.get("Access-Control-Allow-Credentials")).toBe("true");
    expect(response.headers.get("Vary")).toContain("Origin");
  });

  it("exposes the headers range playback needs", () => {
    const response = withCors(new Response("ok"), req("https://app.example.com"), testEnv);
    const exposed = response.headers.get("Access-Control-Expose-Headers") ?? "";
    for (const header of ["Content-Range", "Accept-Ranges", "Content-Length"]) {
      expect(exposed).toContain(header);
    }
  });

  it("adds nothing for a denied origin", () => {
    const response = withCors(new Response("ok"), req("https://evil.com"), testEnv);
    expect(response.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });

  it("never answers with a wildcard", () => {
    const response = withCors(new Response("ok"), req("https://app.example.com"), testEnv);
    expect(response.headers.get("Access-Control-Allow-Origin")).not.toBe("*");
  });

  it("preserves the original status and body", async () => {
    const response = withCors(
      new Response(JSON.stringify({ detail: "nope" }), { status: 403 }),
      req("https://app.example.com"),
      testEnv,
    );
    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({ detail: "nope" });
  });
});

describe("handlePreflight", () => {
  it("answers a real preflight with 204 and the allowed methods", () => {
    const response = handlePreflight(
      req("https://app.example.com", {
        method: "OPTIONS",
        headers: { "Access-Control-Request-Method": "PUT" },
      }),
      testEnv,
    );
    expect(response?.status).toBe(204);
    expect(response?.headers.get("Access-Control-Allow-Methods")).toContain("PUT");
    expect(response?.headers.get("Access-Control-Allow-Headers")).toContain("Range");
  });

  it("403s a preflight from a denied origin", () => {
    const response = handlePreflight(
      req("https://evil.com", {
        method: "OPTIONS",
        headers: { "Access-Control-Request-Method": "PUT" },
      }),
      testEnv,
    );
    expect(response?.status).toBe(403);
  });

  it("passes through a non-preflight OPTIONS", () => {
    expect(handlePreflight(req("https://app.example.com", { method: "OPTIONS" }), testEnv)).toBeNull();
  });

  it("passes through a non-OPTIONS request", () => {
    expect(handlePreflight(req("https://app.example.com"), testEnv)).toBeNull();
  });
});
