import { describe, expect, it } from "vitest";
import { bearerToken, verifyAccessToken } from "../src/auth";

const SECRET = "test-secret-key";
const BEFORE_EXPIRY = 1_899_999_999;

/**
 * Real tokens minted by the backend (`python-jose`, HS256), not by this
 * implementation. They prove the Worker accepts what the API actually issues.
 */
const ACCESS =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTExMTEtMTExMS0xMTExMTExMTExMTEiLCJleHAiOjE5MDAwMDAwMDAsInR5cGUiOiJhY2Nlc3MiLCJyb2xlIjoiY29hY2gifQ.Nj7kOHJN7eah0Ib9x1yR7rFWLX_3tFqskRMHqRm9yuk";
const REFRESH =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTExMTEtMTExMS0xMTExMTExMTExMTEiLCJleHAiOjE5MDAwMDAwMDAsInR5cGUiOiJyZWZyZXNoIn0.UZSyW3nKoNgMvA3t2CmxQXpmNozQkOFAZdnhfsFjlfA";
const EXPIRED =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTExMTEtMTExMS0xMTExMTExMTExMTEiLCJleHAiOjEwMDAwMDAwMDAsInR5cGUiOiJhY2Nlc3MifQ.P42pyr_306A116m16se4mgO1LBy--sbd2tPqKyVcoSE";

function b64url(value: string): string {
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

describe("verifyAccessToken", () => {
  it("accepts a real backend-issued access token", async () => {
    const result = await verifyAccessToken(ACCESS, SECRET, BEFORE_EXPIRY);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.claims.sub).toBe("11111111-1111-1111-1111-111111111111");
      expect(result.claims.role).toBe("coach");
    }
  });

  it("rejects a refresh token", async () => {
    // A refresh token is a valid signature over the same secret. Treating it as
    // a credential would let a stolen refresh token upload film directly.
    expect(await verifyAccessToken(REFRESH, SECRET, BEFORE_EXPIRY)).toEqual({
      ok: false,
      reason: "wrong_type",
    });
  });

  it("rejects an expired token", async () => {
    expect(await verifyAccessToken(EXPIRED, SECRET, BEFORE_EXPIRY)).toEqual({
      ok: false,
      reason: "expired",
    });
  });

  it("rejects a token signed with a different secret", async () => {
    expect(await verifyAccessToken(ACCESS, "not-the-secret", BEFORE_EXPIRY)).toEqual({
      ok: false,
      reason: "bad_signature",
    });
  });

  it('rejects alg "none"', async () => {
    // The classic JWT bug: trusting the token's own algorithm claim.
    const header = b64url(JSON.stringify({ alg: "none", typ: "JWT" }));
    const payload = b64url(JSON.stringify({ sub: "attacker", exp: 1900000000, type: "access" }));
    expect(await verifyAccessToken(`${header}.${payload}.`, SECRET, BEFORE_EXPIRY)).toEqual({
      ok: false,
      reason: "malformed",
    });
  });

  it("rejects a token declaring an asymmetric algorithm", async () => {
    const header = b64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
    const payload = b64url(JSON.stringify({ sub: "attacker", exp: 1900000000, type: "access" }));
    const result = await verifyAccessToken(`${header}.${payload}.sig`, SECRET, BEFORE_EXPIRY);
    expect(result).toEqual({ ok: false, reason: "malformed" });
  });

  it("rejects a tampered payload", async () => {
    const [header, , signature] = ACCESS.split(".");
    const forged = b64url(
      JSON.stringify({ sub: "attacker", exp: 1900000000, type: "access", role: "admin" }),
    );
    expect(await verifyAccessToken(`${header}.${forged}.${signature}`, SECRET, BEFORE_EXPIRY)).toEqual(
      { ok: false, reason: "bad_signature" },
    );
  });

  it.each([null, "", "not-a-jwt", "only.two", "a.b.c.d"])("rejects %s", async (token) => {
    const result = await verifyAccessToken(token, SECRET, BEFORE_EXPIRY);
    expect(result.ok).toBe(false);
  });
});

describe("bearerToken", () => {
  it("extracts a bearer token case-insensitively", () => {
    expect(bearerToken(new Request("https://x/", { headers: { Authorization: "Bearer abc" } }))).toBe(
      "abc",
    );
    expect(bearerToken(new Request("https://x/", { headers: { Authorization: "bearer abc" } }))).toBe(
      "abc",
    );
  });

  it("returns null for a missing or non-bearer header", () => {
    expect(bearerToken(new Request("https://x/"))).toBeNull();
    expect(bearerToken(new Request("https://x/", { headers: { Authorization: "Basic abc" } }))).toBeNull();
    expect(bearerToken(new Request("https://x/", { headers: { Authorization: "Bearer" } }))).toBeNull();
  });
});
