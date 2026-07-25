/**
 * Edge JWT verification.
 *
 * The backend issues HS256 access tokens (`backend/app/auth.py`). Verifying them
 * here lets the Worker reject unauthenticated upload traffic before it reaches
 * -- and wakes -- the container. It is a *pre*-check, never the authorization
 * boundary: the container re-verifies every token and applies the RBAC policy
 * matrix, and the Worker deliberately knows nothing about roles.
 */

const encoder = new TextEncoder();

export interface JwtClaims {
  sub: string;
  exp: number;
  type?: string;
  role?: string;
}

export type VerifyResult =
  | { ok: true; claims: JwtClaims }
  | { ok: false; reason: "missing" | "malformed" | "bad_signature" | "expired" | "wrong_type" };

function base64UrlDecode(input: string): Uint8Array {
  const padded = input.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Pull the bearer token out of an Authorization header. */
export function bearerToken(request: Request): string | null {
  const header = request.headers.get("Authorization");
  if (!header) return null;
  const [scheme, token] = header.split(" ");
  if (!token || scheme.toLowerCase() !== "bearer") return null;
  return token.trim() || null;
}

/**
 * Verify an HS256 access token.
 *
 * Rejects any token whose `type` is not `access` -- refresh tokens are valid
 * signatures over the same secret and must never be usable as credentials.
 */
export async function verifyAccessToken(
  token: string | null,
  secret: string,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): Promise<VerifyResult> {
  if (!token) return { ok: false, reason: "missing" };

  const parts = token.split(".");
  if (parts.length !== 3) return { ok: false, reason: "malformed" };
  const [headerB64, payloadB64, signatureB64] = parts;

  let header: { alg?: string };
  let claims: JwtClaims;
  try {
    header = JSON.parse(new TextDecoder().decode(base64UrlDecode(headerB64)));
    claims = JSON.parse(new TextDecoder().decode(base64UrlDecode(payloadB64)));
  } catch {
    return { ok: false, reason: "malformed" };
  }

  // Pin the algorithm. Accepting whatever `alg` the token declares is the
  // classic JWT confusion bug ("alg": "none", or RS256 verified as HMAC).
  if (header.alg !== "HS256") return { ok: false, reason: "malformed" };

  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  let signature: Uint8Array;
  try {
    signature = base64UrlDecode(signatureB64);
  } catch {
    return { ok: false, reason: "malformed" };
  }
  const valid = await crypto.subtle.verify(
    "HMAC",
    key,
    signature as BufferSource,
    encoder.encode(`${headerB64}.${payloadB64}`),
  );
  if (!valid) return { ok: false, reason: "bad_signature" };

  if (typeof claims.exp !== "number" || claims.exp < nowSeconds) {
    return { ok: false, reason: "expired" };
  }
  if (claims.type !== undefined && claims.type !== "access") {
    return { ok: false, reason: "wrong_type" };
  }
  if (typeof claims.sub !== "string" || !claims.sub) {
    return { ok: false, reason: "malformed" };
  }

  return { ok: true, claims };
}
