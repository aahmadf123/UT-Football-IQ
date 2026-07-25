"use client";

/**
 * Auth provider — client-side session for the static-export frontend.
 *
 * Tokens come from the backend's /api/v1/auth endpoints and persist in
 * localStorage. The provider silently refreshes on mount and on an interval
 * comfortably inside the backend's 60-minute access TTL, and exposes the
 * token to `AppStateProvider` (whose long-standing `authToken` prop was
 * never wired to anything).
 *
 * Mock/offline mode (no NEXT_PUBLIC_API_URL) works tokenless — callers use
 * `authRequired` to decide whether to force the login screen.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import { apiBase } from "./endpoints";

const STORAGE_KEY = "football-iq-auth-v1";
const REFRESH_INTERVAL_MS = 40 * 60 * 1000;

export interface AuthUser {
  email: string;
  role: string;
}

interface StoredAuth {
  token: string;
  refreshToken: string;
  user: AuthUser;
}

export interface AuthContextValue {
  /** Bearer token for API calls; undefined when signed out. */
  token: string | undefined;
  user: AuthUser | null;
  /** False until localStorage has been consulted (avoids redirect flicker). */
  ready: boolean;
  /** True when a backend is configured, so unauthenticated users must sign in. */
  authRequired: boolean;
  /** Force-refreshes access token using refresh token; returns latest token. */
  refreshSession: () => Promise<string | undefined>;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, fullName: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function roleFromToken(token: string): string {
  try {
    const payload = JSON.parse(atob(token.split(".")[1] ?? "")) as { role?: string };
    return payload.role ?? "viewer";
  } catch {
    return "viewer";
  }
}

function loadStored(): StoredAuth | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredAuth>;
    if (!parsed.token || !parsed.refreshToken || !parsed.user) return null;
    return parsed as StoredAuth;
  } catch {
    return null;
  }
}

function saveStored(auth: StoredAuth | null): void {
  if (typeof window === "undefined") return;
  try {
    if (auth) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(auth));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage unavailable (private mode) — session-only auth still works.
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${apiBase()}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    throw new Error(
      "Unable to reach the Football-IQ API. Check your connection or contact an administrator.",
      { cause },
    );
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string") {
        detail = data.detail;
      } else if (Array.isArray(data.detail)) {
        // FastAPI 422 validation errors are a list of {msg, loc, ...}. Surface
        // the actual reasons ("value is not a valid email address", "String
        // should have at least 8 characters") so users can self-correct
        // instead of seeing an opaque "Request failed (422)".
        const msgs = data.detail
          .map((e) => (e && typeof e === "object" ? (e as { msg?: unknown }).msg : null))
          .filter((m): m is string => typeof m === "string");
        if (msgs.length) detail = msgs.join(". ");
      }
    } catch {
      // fall through with the generic message
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

interface TokenPair {
  access_token: string;
  refresh_token: string;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [stored, setStored] = useState<StoredAuth | null>(null);
  const [ready, setReady] = useState(false);
  const storedRef = useRef<StoredAuth | null>(null);
  storedRef.current = stored;

  const applyAuth = useCallback((auth: StoredAuth | null) => {
    setStored(auth);
    saveStored(auth);
  }, []);

  const refresh = useCallback(async (explicit?: StoredAuth): Promise<StoredAuth | null> => {
    // The mount-time call passes the just-loaded session explicitly —
    // storedRef only reflects state after the next render commit.
    const current = explicit ?? storedRef.current;
    if (!current || !apiBase()) return current ?? null;
    try {
      const pair = await postJson<TokenPair>("/api/v1/auth/refresh", {
        refresh_token: current.refreshToken,
      });
      const nextAuth: StoredAuth = {
        token: pair.access_token,
        refreshToken: pair.refresh_token,
        user: { ...current.user, role: roleFromToken(pair.access_token) },
      };
      applyAuth(nextAuth);
      return nextAuth;
    } catch {
      // Refresh token expired/revoked — sign out cleanly.
      applyAuth(null);
      return null;
    }
  }, [applyAuth]);

  const refreshSession = useCallback(async (): Promise<string | undefined> => {
    const next = await refresh();
    return next?.token;
  }, [refresh]);

  useEffect(() => {
    const persisted = loadStored();
    if (persisted) setStored(persisted);
    setReady(true);
    if (persisted && apiBase()) {
      // Validate + rotate in the background; failures clear the session.
      void refresh(persisted);
    }
    const interval = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [refresh]);

  const login = useCallback(
    async (email: string, password: string) => {
      const pair = await postJson<TokenPair>("/api/v1/auth/login", { email, password });
      applyAuth({
        token: pair.access_token,
        refreshToken: pair.refresh_token,
        user: { email, role: roleFromToken(pair.access_token) },
      });
    },
    [applyAuth],
  );

  const register = useCallback(
    async (email: string, password: string, fullName: string) => {
      await postJson<unknown>("/api/v1/auth/register", {
        email,
        password,
        full_name: fullName,
      });
      await login(email, password);
    },
    [login],
  );

  const logout = useCallback(() => applyAuth(null), [applyAuth]);

  const value: AuthContextValue = {
    token: stored?.token,
    user: stored?.user ?? null,
    ready,
    authRequired: Boolean(apiBase()),
    refreshSession,
    login,
    register,
    logout,
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

/**
 * Provider-optional variant for shared chrome (e.g. FootballShell) that unit
 * tests render without the app-level providers. Returns null outside an
 * AuthProvider instead of throwing.
 */
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext);
}
