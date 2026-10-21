import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import fixture from "../../../../tests_local/fixtures/prediction-context.json";
import type { OverlayTracklet } from "@/lib/types";
import { buildPredictionContext, emptyPredictionContext } from "./prediction-context-fields";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.resetModules(); });
beforeEach(() => { vi.stubEnv("NEXT_PUBLIC_USE_MOCKS", ""); vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.test"); });
const tracks: OverlayTracklet[] = Object.entries(fixture.coordinates).map(([id]) => ({ id, player_id: null,
  start_frame: 372, end_frame: 375, track_confidence: 1, team_label: null, position_group: null,
  side_of_ball: ["dl", "lb", "db"].includes(id) ? "defense" : "offense",
  track_points: [{ frame_number: 375, timestamp_seconds: 12.5, shot_id: "native", visibility_state: "observed", bbox: [0, 0, 2, 4], field_x: null, field_y: null, detection_confidence: 1 }],
}));
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
test.each(fixture.cases)("$event_type POST preserves exact source-clock context accepted by the worker contract", async (item) => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const correction = String(input).includes("/api/v1/corrections");
    return { ok: true, status: correction ? 201 : 200, json: async () => correction ? { id: "saved-context" } : [], text: async () => "[]" } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  const [{ CorrectionsPanel }, { AppStateProvider }] = await Promise.all([import("./corrections-panel"), import("@/lib/app-state")]);
  render(<AppStateProvider><CorrectionsPanel clipId="clip" tracklets={tracks} currentTimeSeconds={item.task === "completion" ? 2.499 : 2.5} clipBounds={{ start: 10, end: 20, duration: 10 }} /></AppStateProvider>);
  fireEvent.click(screen.getByTestId("corrections-toggle"));
  change("What needs fixing?", "position_observation");
  change("Observation", item.event_type);
  change("Observed player track", item.actor_id);
  if (item.task === "pressure") {
    for (const [role, id] of Object.entries({ LT: "lt", LG: "lg", C: "center", RG: "rg", RT: "rt" })) change(`Original ${role} track`, id);
    change("Measured LOS field x (yards)", "50"); change("Attacking direction", "1");
  }
  fireEvent.click(screen.getByLabelText("Add prediction-time context"));
  change("Context available at (play seconds)", "2.4");
  change("Observed play design", item.task === "rushing_gain" ? "rush" : "pass");
  for (const id of ["dl", "lb", "db"]) fireEvent.click(screen.getByLabelText(`Prediction defender T${tracks.findIndex((track) => track.id === id) + 1}`));
  fireEvent.click(screen.getByLabelText("I reviewed the complete defender population at this clock"));
  if (item.task === "completion") { change("Prediction quarterback track", "qb"); change("Intended receiver track", "receiver"); }
  if (["yac", "rushing_gain"].includes(item.task)) change("Prediction possessor track", "receiver");
  if (["yac", "completion"].includes(item.task)) fireEvent.click(screen.getByLabelText("I observed an aimed forward pass to an offensive player"));
  if (item.task === "yac") fireEvent.click(screen.getByLabelText("I observed the offensive forward-pass completion at this catch"));
  if (item.task === "pressure") {
    for (const [id, role] of Object.entries({ dl: "DT", lb: "LB", db: "CB" })) change(`Prediction role for T${tracks.findIndex((track) => track.id === id) + 1}`, role);
    change(`Prediction DL gap for T${tracks.findIndex((track) => track.id === "dl") + 1}`, "a");
    fireEvent.click(screen.getByLabelText("I reviewed the complete original front at the snap"));
    change("Call context available at (play seconds)", "2.3");
    fireEvent.click(screen.getByLabelText("These rush, blitz and protection calls were known by the snap; unchecked players mean reviewed none"));
    fireEvent.click(screen.getByLabelText(`Called rusher T${tracks.findIndex((track) => track.id === "dl") + 1}`));
    for (const id of ["lt", "lg", "center", "rg", "rt"]) fireEvent.click(screen.getByLabelText(`Protection blocker T${tracks.findIndex((track) => track.id === id) + 1}`));
    change("Prediction down", "2"); change("Prediction distance (yards)", "7"); change("Measured ball field y (yards)", "0");
  }
  if (item.task === "completion") {
    expect((screen.getByTestId("correction-submit") as HTMLButtonElement).disabled).toBe(true);
    change("Prediction source frame", "12.5");
  }
  fireEvent.click(screen.getByTestId("correction-submit"));
  await waitFor(() => expect(screen.getByTestId("correction-success")).toBeTruthy());
  const call = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/v1/corrections")) as unknown as [string, RequestInit];
  const saved = JSON.parse(String(call[1].body)).corrected_value;
  expect(saved).toMatchObject({ event_type: item.event_type, timestamp_seconds: 12.5, frame_number: 375, timebase: "recording", attributes: { ...item.attributes, actor_id: item.actor_id, shot_id: "native" } });
  expect(saved.attributes).not.toHaveProperty("outcome");
});

test.each(["blank", "future", "unknown_population", "empty_population", "wrong_side", "same_qb_target", "actor_mismatch", "unknown_source", "unknown_shot", "missing_frame", "lost", "duplicate_frame", "different_frame"])("prediction context refuses %s instead of estimating", (fault) => {
  const value = { ...emptyPredictionContext(), enabled: true, available: "2.4", playDesign: "pass", qb: "qb", target: "receiver", forward: true, defenders: ["dl", "lb", "db"], defendersComplete: true };
  const input = { type: "pass_release", value, tracks: structuredClone(tracks), actor: "qb", localTime: 2.5, clipStart: 10 as number | undefined, shot: "native" };
  if (fault === "blank") value.available = "";
  if (fault === "future") value.available = "2.6";
  if (fault === "unknown_population") value.defendersComplete = false;
  if (fault === "empty_population") value.defenders = [];
  if (fault === "wrong_side") value.target = "dl";
  if (fault === "same_qb_target") value.target = "qb";
  if (fault === "actor_mismatch") input.actor = "receiver";
  if (fault === "unknown_source") input.clipStart = undefined;
  if (fault === "unknown_shot") input.shot = "unresolved";
  if (fault === "missing_frame") input.localTime = 2.499;
  const point = input.tracks[0].track_points[0];
  if (fault === "lost") point.visibility_state = "lost";
  if (fault === "duplicate_frame") input.tracks.find((track) => track.id === "qb")!.track_points.push({ ...point });
  if (fault === "different_frame") input.tracks.find((track) => track.id === "qb")!.track_points[0].frame_number = 376;
  // LT is not used in a release snapshot; put loss on an actual required actor.
  if (fault === "lost") input.tracks.find((track) => track.id === "qb")!.track_points[0].visibility_state = "lost";
  expect(buildPredictionContext(input)).toBeNull();
});

test("disabled optional context preserves ordinary observation fields", () => {
  expect(buildPredictionContext({ type: "snap", value: emptyPredictionContext(), tracks: [], actor: "", localTime: 1, shot: "" })).toEqual({ attributes: {} });
});

test("source shot zero and explicit availability zero are valid, without a guessed count", () => {
  const value = { ...emptyPredictionContext(), enabled: true, available: "0", playDesign: "rush", possessor: "receiver", defenders: ["dl"], defendersComplete: true };
  const current = tracks.map((track) => ({ ...track, track_points: track.track_points.map((point) => ({ ...point, shot_id: "00" })) }));
  const output = buildPredictionContext({ type: "handoff", value, tracks: current, actor: "receiver", localTime: 2.5, clipStart: 10, shot: "0" });
  expect(output?.attributes.feature_context_available_at_seconds).toBe(10);
  expect(output?.attributes).not.toHaveProperty("called_blitzer_ids");
});

function pressureInput() {
  return { type: "snap", value: { ...emptyPredictionContext(), enabled: true, available: "2.4", playDesign: "pass", defenders: ["dl", "lb", "db"], defendersComplete: true,
    defensiveRoles: { dl: "DT", lb: "LB", db: "CB" }, gaps: { dl: "a" }, frontComplete: true,
    callsKnown: true, callsAvailable: "2.3", rushers: ["dl"], blitzers: [] as string[], blockers: ["lt", "lg", "center", "rg", "rt"], down: "2", distance: "7", ballY: "0" },
    tracks: structuredClone(tracks), actor: "qb", localTime: 2.5, clipStart: 10, shot: "native",
    originalRoles: { LT: "lt", LG: "lg", C: "center", RG: "rg", RT: "rt" } as Record<string, string>, los: "50", direction: "1" };
}

test("a live playhead change explains a rejected exact-frame save instead of silently doing nothing", async () => {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => ({ ok: true, status: String(input).includes("/corrections") ? 201 : 200, json: async () => [], text: async () => "[]" } as Response));
  vi.stubGlobal("fetch", fetchMock);
  const [{ CorrectionsPanel }, { AppStateProvider }] = await Promise.all([import("./corrections-panel"), import("@/lib/app-state")]);
  render(<AppStateProvider><CorrectionsPanel clipId="clip" tracklets={tracks} currentTimeSeconds={2.5} getCurrentTimeSeconds={() => 2.499} clipBounds={{ start: 10, end: 20, duration: 10 }} /></AppStateProvider>);
  fireEvent.click(screen.getByTestId("corrections-toggle"));
  change("What needs fixing?", "position_observation");
  change("Observation", "pass_release"); change("Observed player track", "qb");
  fireEvent.click(screen.getByLabelText("Add prediction-time context"));
  change("Context available at (play seconds)", "2.4"); change("Observed play design", "pass");
  for (const id of ["dl", "lb", "db"]) fireEvent.click(screen.getByLabelText(`Prediction defender T${tracks.findIndex((track) => track.id === id) + 1}`));
  fireEvent.click(screen.getByLabelText("I reviewed the complete defender population at this clock"));
  change("Prediction quarterback track", "qb"); change("Intended receiver track", "receiver");
  fireEvent.click(screen.getByLabelText("I observed an aimed forward pass to an offensive player"));
  expect((screen.getByTestId("correction-submit") as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByTestId("correction-submit"));
  expect(await screen.findByTestId("correction-error")).toBeTruthy();
  expect(screen.getByTestId("correction-error").textContent).toMatch(/source frame/i);
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/v1/corrections"))).toBe(false);
});
test("pressure preserves explicitly observed optional TE roles without renaming the five OL", () => {
  const input = pressureInput(); input.originalRoles.LTE = "receiver";
  const result = buildPredictionContext(input);
  expect(result?.attributes.ol_roles).toEqual({ ...input.originalRoles });
  expect(result?.attributes.called_blitzer_ids).toEqual([]);
});
test.each(["unknown_calls", "future_call", "empty_blockers", "invalid_blitzer", "wrong_rusher", "unknown_down", "unknown_distance", "unknown_ball", "unknown_gap", "unknown_role", "unknown_front", "duplicate_ol", "unsupported_role", "impossible_los"])("pressure refuses %s", (fault) => {
  const input = pressureInput();
  if (fault === "unknown_calls") input.value.callsKnown = false;
  if (fault === "future_call") input.value.callsAvailable = "2.6";
  if (fault === "empty_blockers") input.value.blockers = [];
  if (fault === "invalid_blitzer") input.value.blitzers = ["lb"];
  if (fault === "wrong_rusher") input.value.rushers = ["qb"];
  if (fault === "unknown_down") input.value.down = "";
  if (fault === "unknown_distance") input.value.distance = "";
  if (fault === "unknown_ball") input.value.ballY = "";
  if (fault === "unknown_gap") input.value.gaps.dl = "";
  if (fault === "unknown_role") input.value.defensiveRoles.lb = "";
  if (fault === "unknown_front") input.value.frontComplete = false;
  if (fault === "duplicate_ol") input.originalRoles.RT = "center";
  if (fault === "unsupported_role") input.originalRoles.RB = "receiver";
  if (fault === "impossible_los") input.los = "999";
  expect(buildPredictionContext(input)).toBeNull();
});
