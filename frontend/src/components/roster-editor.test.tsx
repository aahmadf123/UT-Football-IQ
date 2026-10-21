import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const clients = vi.hoisted(() => ({ fetch: vi.fn(), update: vi.fn(), create: vi.fn(), import: vi.fn(), refresh: vi.fn() }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "admin-token", refreshData: clients.refresh }) }));
vi.mock("@/lib/api", () => ({
  fetchPlayer: clients.fetch,
  createPlayer: clients.create, updatePlayer: clients.update, importPlayers: clients.import,
}));

import { RosterEditor, RosterImport } from "./roster-editor";

beforeEach(() => clients.fetch.mockResolvedValue({ id: "player-1", first_name: "Jordan", last_name: "Smith", jersey_number: 12, position: "QB", position_group: "QB", is_active: true }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("roster changes", () => {
  test("editing a jersey updates the existing player ID and refreshes the roster", async () => {
    const close = vi.fn();
    render(<RosterEditor playerId="player-1" onClose={close} />);
    await waitFor(() => expect((screen.getByLabelText("First name") as HTMLInputElement).value).toBe("Jordan"));
    fireEvent.change(screen.getByLabelText("Jersey number"), { target: { value: "17" } });
    fireEvent.click(screen.getByRole("button", { name: "Save player" }));
    await waitFor(() => expect(clients.update).toHaveBeenCalledWith("player-1", { first_name: "Jordan", last_name: "Smith", jersey_number: 17, position: "QB", position_group: "QB", is_active: true }, "admin-token"));
    expect(clients.create).not.toHaveBeenCalled();
    await waitFor(() => expect(clients.refresh).toHaveBeenCalled());
    expect(close).toHaveBeenCalled();
  });

  test.each(["RB", "WR", "TE"])("saves the distinct %s coaching group on the existing player", async (group) => {
    render(<RosterEditor playerId="player-1" onClose={vi.fn()} />);
    await waitFor(() => expect((screen.getByLabelText("First name") as HTMLInputElement).value).toBe("Jordan"));
    fireEvent.change(screen.getByLabelText("Position"), { target: { value: group } });
    fireEvent.change(screen.getByLabelText("Coaching group"), { target: { value: group } });
    expect((screen.getByLabelText("Coaching group") as HTMLSelectElement).value).toBe(group);
    fireEvent.click(screen.getByRole("button", { name: "Save player" }));
    await waitFor(() => expect(clients.update).toHaveBeenCalledWith("player-1", expect.objectContaining({ position: group, position_group: group }), "admin-token"));
    expect(clients.create).not.toHaveBeenCalled();
  });

  test("CSV conflicts keep the import action disabled after preview", async () => {
    clients.import.mockResolvedValueOnce({ dry_run: true, created: 1, updated: 0, skipped: 0, rows: [{ line: 2, status: "create" }, { line: 3, status: "conflict", error: "Jersey already assigned" }], errors: [{ line: 3, error: "Jersey already assigned" }] });
    render(<RosterImport open onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("CSV contents"), { target: { value: "first_name,last_name\nJordan,Smith\nJordan,Jones" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview roster" }));
    await waitFor(() => expect(screen.getByText(/Jersey already assigned/)).toBeTruthy());
    expect((screen.getByRole("button", { name: "Import 1 players" }) as HTMLButtonElement).disabled).toBe(true);
    expect(clients.import).toHaveBeenCalledTimes(1);
    expect(clients.import.mock.calls[0][1]).toBe(true);
  });

  test("a clean preview is explicitly applied and then refreshed", async () => {
    const result = { dry_run: true, created: 1, updated: 0, skipped: 0, rows: [{ line: 2, status: "create" }], errors: [] };
    clients.import.mockResolvedValueOnce(result).mockResolvedValueOnce({ ...result, dry_run: false });
    render(<RosterImport open onClose={vi.fn()} />);
    const csv = "first_name,last_name\nJordan,Smith";
    fireEvent.change(screen.getByLabelText("CSV contents"), { target: { value: csv } });
    fireEvent.click(screen.getByRole("button", { name: "Preview roster" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Import 1 players" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Import 1 players" }));
    await waitFor(() => expect(clients.import).toHaveBeenLastCalledWith(csv, false, "admin-token"));
    await waitFor(() => expect(screen.getByText(/Import complete/)).toBeTruthy());
    expect(clients.refresh).toHaveBeenCalled();
  });
});
