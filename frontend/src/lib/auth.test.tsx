// @vitest-environment jsdom
import { act, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { AuthProvider, useAuth } from "./auth";

function Probe() {
  const { token, user, ready, authRequired, login, logout } = useAuth();
  return (
    <div>
      <span data-testid="ready">{String(ready)}</span>
      <span data-testid="required">{String(authRequired)}</span>
      <span data-testid="token">{token ?? "none"}</span>
      <span data-testid="email">{user?.email ?? "none"}</span>
      <span data-testid="role">{user?.role ?? "none"}</span>
      <button data-testid="login" onClick={() => void login("coach@example.com", "pw")} />
      <button data-testid="logout" onClick={logout} />
    </div>
  );
}

function makeJwt(role: string): string {
  const payload = btoa(JSON.stringify({ sub: "u1", role }));
  return `hdr.${payload}.sig`;
}

describe("AuthProvider", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("login stores tokens, decodes role, and logout clears", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        access_token: makeJwt("coach"),
        refresh_token: "refresh-1",
      }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("true"));
    expect(screen.getByTestId("required").textContent).toBe("true");
    expect(screen.getByTestId("token").textContent).toBe("none");

    await act(async () => {
      screen.getByTestId("login").click();
    });
    await waitFor(() =>
      expect(screen.getByTestId("email").textContent).toBe("coach@example.com"),
    );
    expect(screen.getByTestId("role").textContent).toBe("coach");
    expect(screen.getByTestId("token").textContent).not.toBe("none");
    expect(
      JSON.parse(window.localStorage.getItem("football-iq-auth-v1") ?? "{}").refreshToken,
    ).toBe("refresh-1");

    await act(async () => {
      screen.getByTestId("logout").click();
    });
    expect(screen.getByTestId("token").textContent).toBe("none");
    expect(window.localStorage.getItem("football-iq-auth-v1")).toBeNull();
  });

  test("persisted session hydrates and refresh rotates the token", async () => {
    window.localStorage.setItem(
      "football-iq-auth-v1",
      JSON.stringify({
        token: makeJwt("analyst"),
        refreshToken: "refresh-old",
        user: { email: "a@example.com", role: "analyst" },
      }),
    );
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        access_token: makeJwt("analyst"),
        refresh_token: "refresh-new",
      }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("email").textContent).toBe("a@example.com"));
    await waitFor(() =>
      expect(
        JSON.parse(window.localStorage.getItem("football-iq-auth-v1") ?? "{}").refreshToken,
      ).toBe("refresh-new"),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.test/api/v1/auth/refresh",
      expect.objectContaining({ method: "POST" }),
    );
  });

  test("failed refresh signs the user out", async () => {
    window.localStorage.setItem(
      "football-iq-auth-v1",
      JSON.stringify({
        token: makeJwt("coach"),
        refreshToken: "expired",
        user: { email: "c@example.com", role: "coach" },
      }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })),
    );

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("token").textContent).toBe("none"));
    expect(window.localStorage.getItem("football-iq-auth-v1")).toBeNull();
  });

  test("surfaces FastAPI 422 validation messages instead of a generic error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 422,
        json: async () => ({
          detail: [
            { msg: "value is not a valid email address", loc: ["body", "email"] },
            { msg: "String should have at least 8 characters", loc: ["body", "password"] },
          ],
        }),
      })),
    );

    function ErrProbe() {
      const { register } = useAuth();
      const [err, setErr] = useState("none");
      return (
        <>
          <span data-testid="err">{err}</span>
          <button
            data-testid="reg"
            onClick={() => register("bad", "short", "N").catch((e) => setErr(e.message))}
          />
        </>
      );
    }

    render(
      <AuthProvider>
        <ErrProbe />
      </AuthProvider>,
    );
    await act(async () => {
      screen.getByTestId("reg").click();
    });
    await waitFor(() =>
      expect(screen.getByTestId("err").textContent).toContain("valid email address"),
    );
    // Both validation reasons are surfaced, not a bare "Request failed (422)".
    expect(screen.getByTestId("err").textContent).toContain("at least 8 characters");
    expect(screen.getByTestId("err").textContent).not.toContain("Request failed");
  });

  test("replaces browser network failures with actionable guidance", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));

    function ErrProbe() {
      const { login } = useAuth();
      const [err, setErr] = useState("none");
      return (
        <>
          <span data-testid="err">{err}</span>
          <button
            data-testid="login"
            onClick={() => login("coach@example.com", "password").catch((e) => setErr(e.message))}
          />
        </>
      );
    }

    render(
      <AuthProvider>
        <ErrProbe />
      </AuthProvider>,
    );
    await act(async () => {
      screen.getByTestId("login").click();
    });
    await waitFor(() =>
      expect(screen.getByTestId("err").textContent).toBe(
        "Unable to reach the Football-IQ API. Check your connection or contact an administrator.",
      ),
    );
  });

  test("mock mode (no API base) does not require auth", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("true"));
    expect(screen.getByTestId("required").textContent).toBe("false");
  });
});
