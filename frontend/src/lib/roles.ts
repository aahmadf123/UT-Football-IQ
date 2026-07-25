// Client-side role awareness for surface gating (Issue #113).
//
// IMPORTANT: nothing here is a security boundary. The backend re-checks every
// request against its RBAC policy (app.governance.POLICY). These helpers only
// decide which navigation entries and surfaces to *show*, so a sensitive
// surface (Health & Workload) stays hidden unless the caller looks approved.

export const USER_ROLES = [
  "admin",
  "analyst",
  "coach",
  "sportsperformance",
  "player",
  "viewer",
] as const;

export type UserRole = (typeof USER_ROLES)[number];

// Roles approved to view the Health & Workload surface. Mirrors the backend
// policy POLICY[(HEALTH_WORKLOAD, READ)] — sports-performance / S&C / wellness
// staff plus analytics leads and admins. Coaches and player/viewer accounts
// are intentionally excluded.
export const HEALTH_WORKLOAD_ROLES: readonly UserRole[] = ["admin", "analyst", "sportsperformance"];

export function isUserRole(value: unknown): value is UserRole {
  return typeof value === "string" && (USER_ROLES as readonly string[]).includes(value);
}

export function canAccessHealthWorkload(role: UserRole | null | undefined): boolean {
  return role != null && HEALTH_WORKLOAD_ROLES.includes(role);
}

// Roles approved to see PLAYER-LEVEL workload risk (Issue #149): the
// sports-performance ("athletic training") track plus admins. Analysts keep
// surface access but only see position-group aggregates — mirrors the
// backend's PLAYER_LEVEL_RISK_ROLES in app/routers/health_workload.py.
export const PLAYER_LEVEL_RISK_ROLES: readonly UserRole[] = ["admin", "sportsperformance"];

export function canSeePlayerLevelRisk(role: UserRole | null | undefined): boolean {
  return role != null && PLAYER_LEVEL_RISK_ROLES.includes(role);
}

// Roles allowed to submit coach corrections (POST /api/v1/corrections).
// Mirrors the backend's ``require_coach_or_above`` guard — player / viewer /
// sports-performance accounts get a 403, so the UI disables the form and says
// so instead of letting the request fail.
export const CORRECTION_ROLES: readonly UserRole[] = ["admin", "analyst", "coach"];

export function canSubmitCorrections(role: UserRole | null | undefined): boolean {
  return role != null && CORRECTION_ROLES.includes(role);
}

// Decode the `role` claim from a JWT access token *without* verifying the
// signature. The browser never trusts this for authorization; it only uses it
// to decide what to render. Returns null for an absent or malformed token.
export function decodeRoleFromToken(token: string | undefined | null): UserRole | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length < 2) return null;
  try {
    let payload = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    while (payload.length % 4 !== 0) payload += "=";
    const json =
      typeof atob === "function"
        ? atob(payload)
        : Buffer.from(payload, "base64").toString("binary");
    const claims = JSON.parse(json) as { role?: unknown };
    return isUserRole(claims.role) ? claims.role : null;
  } catch {
    return null;
  }
}

function readDemoRole(): UserRole | null {
  // Build-time/demo override so screenshots and walkthroughs can preview the
  // approved-role experience without a real login flow.
  const raw = process.env.NEXT_PUBLIC_DEMO_ROLE;
  return isUserRole(raw) ? raw : null;
}

// Resolve the effective role used for client-side surface gating:
//   1. the JWT role claim when signed in (re-verified by the backend),
//   2. an explicit NEXT_PUBLIC_DEMO_ROLE override, else
//   3. a safe default of "coach" — the primary coach-facing persona, which is
//      NOT approved for Health & Workload, so the surface stays hidden until a
//      real session proves an approved role.
export function resolveCurrentRole(token?: string | null): UserRole {
  return decodeRoleFromToken(token) ?? readDemoRole() ?? "coach";
}
