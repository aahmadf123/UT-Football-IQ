// @vitest-environment jsdom
// Auth-boundary behavior of FootballShell: signed-out visitors on a
// backend-configured deployment are sent to /login/ exactly once — never
// from the login page itself. A static host that mis-serves /login/ with
// the app shell must not produce an infinite reload loop (each iteration
// hammered the API with unauthenticated requests).
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { AuthProvider } from "@/lib/auth";
import { AppStateProvider } from "@/lib/app-state";
import { FootballShell } from "./app-shell";

vi.mock("next/link", () => ({
  default: ({ children, ...rest }: React.ComponentProps<"a">) => <a {...rest}>{children}</a>,
}));

vi.mock("next/image", () => ({
  default: () => null,
}));

const originalLocation = window.location;

function stubLocation(pathname: string): ReturnType<typeof vi.fn> {
  const replace = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...originalLocation, pathname, replace },
  });
  return replace;
}

function renderShell() {
  return render(
    <AuthProvider>
      <AppStateProvider>
        <FootballShell activePage="dashboard">
          <div />
        </FootballShell>
      </AppStateProvider>
    </AuthProvider>,
  );
}

describe("FootballShell auth boundary", () => {
  beforeEach(() => {
    // A configured backend makes auth required; no stored session = signed out.
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test");
    window.localStorage.clear();
    // Widgets in the shell (alerts bell, inbox) fetch on mount; answer 401.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 401,
        json: async () => ({ detail: "Not authenticated" }),
      })),
    );
  });

  afterEach(() => {
    cleanup();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    });
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("signed-out visitor on an app page is sent to /login/", async () => {
    const replace = stubLocation("/");
    renderShell();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login/"));
  });

  test("never redirects while already on /login/ (no reload loop)", async () => {
    const replace = stubLocation("/login/");
    renderShell();
    // The redirect effect runs after mount; give it the same window the
    // positive case needs, then assert it stayed put.
    await new Promise((r) => setTimeout(r, 50));
    expect(replace).not.toHaveBeenCalled();
  });

  test("never redirects from /login without trailing slash", async () => {
    const replace = stubLocation("/login");
    renderShell();
    await new Promise((r) => setTimeout(r, 50));
    expect(replace).not.toHaveBeenCalled();
  });
});
