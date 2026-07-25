/**
 * HMAC stream-URL signing, byte-compatible with the backend.
 *
 * The backend mints playback URLs that a bare `<video>` tag can fetch, which
 * rules out an Authorization header. `backend/app/storage.py` signs them like
 * this, and this module must stay in lockstep with it:
 *
 *     streamKey = HMAC-SHA256(SECRET_KEY, "football-iq:stream-signing")
 *     sig       = hex(HMAC-SHA256(streamKey, `${bucket}:${key}:${exp}`))
 *
 * `key` is the *decoded* object key -- the URL percent-encodes each path
 * segment, so callers must decode before verifying or every signature fails.
 */

const STREAM_KEY_CONTEXT = "football-iq:stream-signing";

const encoder = new TextEncoder();

async function hmac(key: ArrayBuffer | Uint8Array, message: string): Promise<ArrayBuffer> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key as BufferSource,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(message));
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Derive the stream-signing subkey from the shared app secret. */
async function derivedStreamKey(secret: string): Promise<ArrayBuffer> {
  return hmac(encoder.encode(secret), STREAM_KEY_CONTEXT);
}

/** Compute the signature for `bucket/key` expiring at `exp` (unix seconds). */
export async function signStreamPath(
  secret: string,
  bucket: string,
  key: string,
  exp: number,
): Promise<string> {
  const streamKey = await derivedStreamKey(secret);
  return toHex(await hmac(streamKey, `${bucket}:${key}:${exp}`));
}

/**
 * Compare two hex digests without leaking their contents through timing.
 *
 * Both are attacker-visible hex strings of known length, so the only thing worth
 * hiding is *where* they first differ.
 */
export function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export type SignatureCheck =
  | { ok: true }
  | { ok: false; reason: "missing" | "malformed_exp" | "expired" | "bad_signature" };

/**
 * Verify a signed stream URL.
 *
 * `nowSeconds` is injectable so expiry can be tested without faking the clock.
 */
export async function verifyStreamSignature(
  secret: string,
  bucket: string,
  key: string,
  exp: string | null,
  sig: string | null,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): Promise<SignatureCheck> {
  if (!exp || !sig) return { ok: false, reason: "missing" };

  // Number() accepts "" and "0x10"; require a plain run of digits so a
  // signature can never be replayed with a creatively-formatted expiry.
  if (!/^\d+$/.test(exp)) return { ok: false, reason: "malformed_exp" };
  const expNum = Number(exp);
  if (!Number.isSafeInteger(expNum)) return { ok: false, reason: "malformed_exp" };
  if (expNum < nowSeconds) return { ok: false, reason: "expired" };

  const expected = await signStreamPath(secret, bucket, key, expNum);
  return timingSafeEqual(expected, sig) ? { ok: true } : { ok: false, reason: "bad_signature" };
}

/**
 * Reject keys that attempt path traversal.
 *
 * Mirrors `is_valid_key` in `backend/app/storage.py`. R2 is a flat keyspace so
 * `..` is not *itself* dangerous there, but keeping the two in agreement means a
 * key that the backend refuses to write can never be read back through the edge.
 */
export function isValidKey(key: string): boolean {
  if (!key) return false;
  if (key.includes("\0")) return false;
  if (key.startsWith("/")) return false;
  return !key.includes("..");
}
