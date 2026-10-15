import { afterEach, describe, expect, test, vi } from "vitest";
import { fetchTeamAccounts, updateTeamAccountRole } from "./team-access";

vi.mock("./endpoints", () => ({ apiBase: () => "https://api.example.test" }));
afterEach(() => { vi.unstubAllGlobals(); });

describe("team access client", () => {
  test("uses authenticated account endpoints and sends only the selected role", async () => {
    const request = vi.fn(async () => ({ ok: true, json: async () => [] }));
    vi.stubGlobal("fetch", request);
    await fetchTeamAccounts("staff-session");
    expect(request).toHaveBeenLastCalledWith("https://api.example.test/api/v1/auth/users", { method: "GET", headers: { Authorization: "Bearer staff-session" } });
    await updateTeamAccountRole("staff-1", "coach", "staff-session");
    expect(request).toHaveBeenLastCalledWith("https://api.example.test/api/v1/auth/users/staff-1/role", { method: "PATCH", headers: { Authorization: "Bearer staff-session", "Content-Type": "application/json" }, body: JSON.stringify({ role: "coach" }) });
  });

  test("preserves the server's last-administrator conflict message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 409, json: async () => ({ detail: "Cannot demote the last remaining admin" }) })));
    await expect(updateTeamAccountRole("admin-1", "viewer", "staff-session")).rejects.toThrow("Cannot demote the last remaining admin");
  });
});
