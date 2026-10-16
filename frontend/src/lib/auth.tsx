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
 * Explicit demo mode works tokenless. Local and hosted libraries both use
 * the backend authentication API.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import { apiBase, configureApiUrl } from "./endpoints";
import { ApiError } from "./api-error";
import { useMocks } from "./mock-flag";

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

class AuthRequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
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

async function postJson<T>(path: string, body: unknown, origin = apiBase()): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${origin}${path}`, {
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
    let detail = new ApiError({ status: res.status, action: "sign in" }).message;
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
    throw new AuthRequestError(detail, res.status);
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
  const localBootstrapRef = useRef(false);
  const mockMode = useMocks();
  storedRef.current = stored;

  const applyAuth = useCallback((auth: StoredAuth | null) => {
    setStored(auth);
    saveStored(auth);
  }, []);

  const refresh = useCallback(async (explicit?: StoredAuth): Promise<StoredAuth | null> => {
    // The mount-time call passes the just-loaded session explicitly —
    // storedRef only reflects state after the next render commit.
    const current = explicit ?? storedRef.current;
    if (!current) return null;
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
    } catch (cause) {
      if (cause instanceof AuthRequestError && [401, 403].includes(cause.status)) {
        applyAuth(null);
        return null;
      }
      // A restarting local API or a network interruption does not revoke a session.
      return current;
    }
  }, [applyAuth]);

  const refreshSession = useCallback(async (): Promise<string | undefined> => {
    const next = await refresh();
    return next?.token;
  }, [refresh]);

  useEffect(() => {
    if (localBootstrapRef.current) return;
    const url = new URL(window.location.href);
    const localToken = url.searchParams.get("localToken");
    if (localToken) {
      url.searchParams.delete("localToken");
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    }
    if (localToken && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) {
      localBootstrapRef.current = true;
      void (async () => {
        try {
          // A launch token always belongs to the API that served this local
          // page. A saved team connection never receives this secret.
          const pair = await postJson<TokenPair>("/api/v1/local/login", { bootstrap_token: localToken }, "");
          const response = await fetch("/api/v1/auth/me", { headers: { Authorization: `Bearer ${pair.access_token}` } });
          if (!response.ok) throw new Error("Could not load the local account.");
          const user = await response.json() as AuthUser;
          if (apiBase()) configureApiUrl("");
          applyAuth({ token: pair.access_token, refreshToken: pair.refresh_token, user: { email: user.email, role: user.role } });
        } catch {
          // The normal login screen remains usable when a launch token expired.
          applyAuth(null);
        } finally {
          setReady(true);
        }
      })();
      return;
    }
    const persisted = loadStored();
    if (persisted) setStored(persisted);
    setReady(true);
    if (persisted && !mockMode) {
      // Validate + rotate in the background; failures clear the session.
      void refresh(persisted);
    }
  }, [refresh, applyAuth, mockMode]);

  useEffect(() => {
    if (!ready || mockMode) return;
    const interval = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [ready, mockMode, refresh]);

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
    authRequired: !mockMode,
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
