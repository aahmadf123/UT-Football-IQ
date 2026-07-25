import { afterEach, describe, expect, test, vi } from "vitest";

import { apiBase } from "./endpoints";

describe("apiBase", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  test("uses an explicitly configured API origin", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.test/");

    expect(apiBase()).toBe("https://api.example.test");
  });

  test("uses the deployed backend when a production static build omits the variable", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    vi.stubEnv("NODE_ENV", "production");

    expect(apiBase()).toBe("https://football-iq-backend.fly.dev");
  });

  test("keeps development and test builds offline when no origin is configured", () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    vi.stubEnv("NODE_ENV", "test");

    expect(apiBase()).toBe("");
  });
});
