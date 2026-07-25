import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import {
  canAccessHealthWorkload,
  decodeRoleFromToken,
  resolveCurrentRole,
} from "./roles";

// Build an unsigned JWT-shaped token with the given payload (header.payload.sig).
function fakeJwt(payload: Record<string, unknown>): string {
  const b64 = (obj: Record<string, unknown>) =>
    Buffer.from(JSON.stringify(obj)).toString("base64").replace(/=+$/, "");
  return `${b64({ alg: "HS256", typ: "JWT" })}.${b64(payload)}.sig`;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "");
});

describe("canAccessHealthWorkload", () => {
  test("approves sports-performance, analyst, and admin", () => {
    expect(canAccessHealthWorkload("sportsperformance")).toBe(true);
    expect(canAccessHealthWorkload("analyst")).toBe(true);
    expect(canAccessHealthWorkload("admin")).toBe(true);
  });

  test("denies coach, player, viewer, and unknown", () => {
    expect(canAccessHealthWorkload("coach")).toBe(false);
    expect(canAccessHealthWorkload("player")).toBe(false);
    expect(canAccessHealthWorkload("viewer")).toBe(false);
    expect(canAccessHealthWorkload(null)).toBe(false);
    expect(canAccessHealthWorkload(undefined)).toBe(false);
  });
});

describe("decodeRoleFromToken", () => {
  test("reads the role claim from a JWT", () => {
    expect(decodeRoleFromToken(fakeJwt({ sub: "u1", role: "sportsperformance" }))).toBe(
      "sportsperformance",
    );
  });

  test("returns null for unknown role, malformed, or missing token", () => {
    expect(decodeRoleFromToken(fakeJwt({ role: "wizard" }))).toBeNull();
    expect(decodeRoleFromToken("not-a-jwt")).toBeNull();
    expect(decodeRoleFromToken(undefined)).toBeNull();
  });
});

describe("resolveCurrentRole", () => {
  test("prefers the token role over the demo override", () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "coach");
    expect(resolveCurrentRole(fakeJwt({ role: "admin" }))).toBe("admin");
  });

  test("falls back to NEXT_PUBLIC_DEMO_ROLE when there is no token", () => {
    vi.stubEnv("NEXT_PUBLIC_DEMO_ROLE", "analyst");
    expect(resolveCurrentRole(undefined)).toBe("analyst");
  });

  test("defaults to coach (a non-approved role) when nothing is set", () => {
    expect(resolveCurrentRole(undefined)).toBe("coach");
    expect(canAccessHealthWorkload(resolveCurrentRole(undefined))).toBe(false);
  });
});
