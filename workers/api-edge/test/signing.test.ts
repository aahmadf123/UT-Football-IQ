import { describe, expect, it } from "vitest";
import { isValidKey, signStreamPath, timingSafeEqual, verifyStreamSignature } from "../src/signing";

const SECRET = "test-secret-key";

/**
 * Fixtures produced by the real backend, not by this implementation:
 *
 *     SECRET_KEY=test-secret-key python -c \
 *       "from app.storage import sign_stream_path; print(sign_stream_path(...))"
 *
 * They are the contract between the two languages. If a change here makes these
 * fail, the Worker has stopped accepting URLs the backend mints -- every video
 * in the app goes 403. Regenerate them only when the backend's scheme changes
 * on purpose.
 */
const BACKEND_FIXTURES = [
  {
    bucket: "raw-video",
    key: "raw/1750000000000-practice.mp4",
    exp: 1900000000,
    sig: "d9b7edeb00588306ab3b183edb8d50063769492f316f1209d7a48d07a6d8164e",
  },
  {
    // Spaces in the key: the URL percent-encodes them, but the signature is
    // over the decoded form. Getting this backwards breaks every clip whose
    // filename has a space -- which is all of the DJI footage.
    bucket: "clips",
    key: "clips/abc def/play 01.mp4",
    exp: 1900000000,
    sig: "44dff235e31109cb38716016072ca496ac439f6dda64f706c5d264128813e52e",
  },
  {
    bucket: "overlays",
    key: "overlays/x.mp4",
    exp: 2000000000,
    sig: "385183b10b140ae834412bc05d5b4ab3d029ad8c99e71a47564a17672c0ffb75",
  },
];

describe("signStreamPath", () => {
  it.each(BACKEND_FIXTURES)(
    "matches the Python backend for $bucket/$key",
    async ({ bucket, key, exp, sig }) => {
      await expect(signStreamPath(SECRET, bucket, key, exp)).resolves.toBe(sig);
    },
  );

  it("changes when any component changes", async () => {
    const base = await signStreamPath(SECRET, "clips", "a.mp4", 1900000000);
    const others = await Promise.all([
      signStreamPath(SECRET, "overlays", "a.mp4", 1900000000),
      signStreamPath(SECRET, "clips", "b.mp4", 1900000000),
      signStreamPath(SECRET, "clips", "a.mp4", 1900000001),
      signStreamPath("другой", "clips", "a.mp4", 1900000000),
    ]);
    for (const other of others) expect(other).not.toBe(base);
  });
});

describe("verifyStreamSignature", () => {
  const { bucket, key, exp, sig } = BACKEND_FIXTURES[0];
  const before = exp - 1;

  it("accepts a valid unexpired signature", async () => {
    const result = await verifyStreamSignature(SECRET, bucket, key, String(exp), sig, before);
    expect(result).toEqual({ ok: true });
  });

  it("rejects an expired signature even though it verifies", async () => {
    const result = await verifyStreamSignature(SECRET, bucket, key, String(exp), sig, exp + 1);
    expect(result).toEqual({ ok: false, reason: "expired" });
  });

  it("rejects a signature minted for a different key", async () => {
    const result = await verifyStreamSignature(
      SECRET,
      bucket,
      "raw/someone-elses-film.mp4",
      String(exp),
      sig,
      before,
    );
    expect(result).toEqual({ ok: false, reason: "bad_signature" });
  });

  it("rejects a signature minted for a different bucket", async () => {
    const result = await verifyStreamSignature(SECRET, "clips", key, String(exp), sig, before);
    expect(result).toEqual({ ok: false, reason: "bad_signature" });
  });

  it("rejects a signature made with a different secret", async () => {
    const result = await verifyStreamSignature("wrong-secret", bucket, key, String(exp), sig, before);
    expect(result).toEqual({ ok: false, reason: "bad_signature" });
  });

  it.each([null, ""])("rejects a missing signature (%s)", async (value) => {
    expect(await verifyStreamSignature(SECRET, bucket, key, String(exp), value, before)).toEqual({
      ok: false,
      reason: "missing",
    });
  });

  it("rejects a missing expiry", async () => {
    expect(await verifyStreamSignature(SECRET, bucket, key, null, sig, before)).toEqual({
      ok: false,
      reason: "missing",
    });
  });

  it.each(["0x7000000000", "1e12", " 1900000000", "1900000000.0", "-1", "abc"])(
    "rejects a non-decimal expiry (%s)",
    async (badExp) => {
      // Number() would happily parse several of these, which would let an
      // attacker re-present a captured signature with a re-encoded expiry.
      const result = await verifyStreamSignature(SECRET, bucket, key, badExp, sig, before);
      expect(result.ok).toBe(false);
    },
  );
});

describe("timingSafeEqual", () => {
  it("is true only for identical strings", () => {
    expect(timingSafeEqual("abc123", "abc123")).toBe(true);
    expect(timingSafeEqual("abc123", "abc124")).toBe(false);
    expect(timingSafeEqual("abc123", "abc12")).toBe(false);
    expect(timingSafeEqual("", "")).toBe(true);
  });
});

describe("isValidKey", () => {
  it.each(["raw/1-a.mp4", "clips/x/y/z.mp4", "a", "with space.mp4"])("accepts %s", (key) => {
    expect(isValidKey(key)).toBe(true);
  });

  it.each(["", "/leading", "../escape", "raw/../../etc/passwd", "nul\0byte"])(
    "rejects %s",
    (key) => {
      expect(isValidKey(key)).toBe(false);
    },
  );
});
