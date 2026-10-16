import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { TeamAccount } from "@/lib/team-access";

const clients = vi.hoisted(() => ({ list: vi.fn(), update: vi.fn(), refresh: vi.fn(), role: "admin" }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ currentRole: clients.role, authToken: "admin-token" }) }));
vi.mock("@/lib/auth", () => ({ useOptionalAuth: () => ({ user: { email: "admin@example.test" }, refreshSession: clients.refresh }) }));
vi.mock("@/lib/team-access", () => ({ fetchTeamAccounts: clients.list, updateTeamAccountRole: clients.update }));
import { TeamAccessPanel } from "./team-access-panel";

const admin: TeamAccount = { id: "admin-1", email: "admin@example.test", full_name: "Team administrator", role: "admin", is_active: true };
const teammate: TeamAccount = { id: "staff-1", email: "staff@example.test", full_name: "Staff member", role: "viewer", is_active: true };

beforeEach(() => { clients.role = "admin"; clients.list.mockResolvedValue([admin, teammate]); clients.refresh.mockResolvedValue("refreshed-token"); });
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("team access", () => {
  test("does not list accounts or expose controls to non-administrators", () => {
    clients.role = "coach";
    render(<TeamAccessPanel />);
    expect(screen.queryByRole("heading", { name: "Team access" })).toBeNull();
    expect(clients.list).not.toHaveBeenCalled();
  });

  test("promotes a registered viewer with independent pending and dirty state per account", async () => {
    let complete!: (account: TeamAccount) => void;
    clients.update.mockReturnValueOnce(new Promise<TeamAccount>((resolve) => { complete = resolve; }));
    render(<TeamAccessPanel />);
    const role = await screen.findByLabelText(`Role for ${teammate.email}`);
    // On a slow runner the rows can render a beat before loading settles;
    // interacting then is reset by the load completing.
    await waitFor(() => expect(screen.queryByText("Loading team accounts…")).toBeNull());
    const save = screen.getByRole("button", { name: `Save access for ${teammate.email}` }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(role, { target: { value: "coach" } });
    await waitFor(() => expect(save.disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(clients.update).toHaveBeenCalledWith(teammate.id, "coach", "admin-token"));
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(`Role for ${admin.email}`), { target: { value: "analyst" } });
    expect((screen.getByRole("button", { name: `Save access for ${admin.email}` }) as HTMLButtonElement).disabled).toBe(false);
    complete({ ...teammate, role: "coach" });
    await screen.findByText("Access saved.");
    expect((role as HTMLSelectElement).value).toBe("coach");
    expect(save.disabled).toBe(true);
    expect(clients.refresh).not.toHaveBeenCalled();
  });

  test("preserves a last-administrator error and refreshes the current session only after a successful retry", async () => {
    clients.update.mockRejectedValueOnce(new Error("Cannot demote the last remaining admin"));
    render(<TeamAccessPanel />);
    const role = await screen.findByLabelText(`Role for ${admin.email}`);
    fireEvent.change(role, { target: { value: "viewer" } });
    const save = screen.getByRole("button", { name: `Save access for ${admin.email}` });
    fireEvent.click(save);
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Cannot demote the last remaining admin");
    expect((role as HTMLSelectElement).value).toBe("viewer");
    expect((save as HTMLButtonElement).disabled).toBe(false);
    expect(clients.refresh).not.toHaveBeenCalled();
    clients.update.mockResolvedValueOnce({ ...admin, role: "viewer" });
    fireEvent.click(save);
    await waitFor(() => expect(clients.refresh).toHaveBeenCalledTimes(1));
    await screen.findByText("Access saved.");
  });

  test("recovers from a list request error without presenting sample accounts", async () => {
    clients.list.mockRejectedValueOnce(new Error("Access denied (403)"));
    render(<TeamAccessPanel />);
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("doesn't have access"));
    expect(screen.queryByRole("combobox")).toBeNull();
    clients.list.mockResolvedValueOnce([]);
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "Try again" }));
    await screen.findByText("No registered accounts were returned by this library.");
    expect(screen.queryByRole("combobox")).toBeNull();
  });
});
