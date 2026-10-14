import { afterEach, describe, expect, test, vi } from "vitest";

import { apiBase } from "./endpoints";

describe("apiBase", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    delete window.__FOOTBALL_IQ_CONFIG__;
  });

  test("uses an explicitly configured API origin", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.test/");

    expect(apiBase()).toBe("https://api.example.test");
  });

  test("stays relative in production when no origin is configured", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    vi.stubEnv("NODE_ENV", "production");

    expect(apiBase()).toBe("");
  });

  test("keeps development and test requests on the local origin", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    vi.stubEnv("NODE_ENV", "test");

    expect(apiBase()).toBe("");
  });

  test("runtime local configuration overrides the build-time API origin", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://old.example.test");
    window.__FOOTBALL_IQ_CONFIG__ = { apiUrl: "http://localhost:8080/" };
    expect(apiBase()).toBe("http://localhost:8080");
  });
});
