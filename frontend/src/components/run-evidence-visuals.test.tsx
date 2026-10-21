import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AnalyticsGroup } from "@/lib/analytics-api";
import fixture from "./__fixtures__/run-evidence-producer.json";
import { observedRunEvidence, runDensity, RunEvidenceVisuals } from "./run-evidence-visuals";
afterEach(cleanup);
function groups(): AnalyticsGroup[] { return fixture.rows.filter((r) => r.metric_name !== "observed_run_path").map((r, i) => ({ key: r.metric_name, metric_name: r.metric_name, unit: r.unit, n: 1, mean: 1, median: 1, p10: 1, p90: 1, min: 1, max: 1, low_sample: true, definition_version: r.metric_value.definition_version, evidence: [{ play_id: "play", clip_id: "clip", video_id: "video", metric_id: String(i), value: 1, start_seconds: 0, end_seconds: 3, tracklet_id: "rb", result_version: "job", metric_packet: structuredClone(r.metric_value) }] })); }
function one() { return [groups()[0]]; }

test("actual producer packets supply one carry, original named gaps and explicit contact", () => {
  const runs = observedRunEvidence(groups()); expect(runs).toHaveLength(1);
  expect(runs[0].contact).toMatchObject({ seconds: 1.2, x: 50.2, y: 1 });
  expect(runs[0].entry).toMatchObject({ gap: "left_A", seconds: 1 });
  expect(runs[0].front?.roles.find((p) => p.role === "C")?.id).toBe("c");
  expect(runs[0].partial).toBe(false);
});
test("density counts distinct plays, not samples, duplicate metrics or FPS", () => {
  const runs = observedRunEvidence([...groups(), ...groups()]);
  expect(runDensity(runs).every((cell) => cell.plays.size === 1)).toBe(true);
  const second = { ...runs[0], row: { ...runs[0].row, play_id: "second" } };
  expect(runDensity([...runs, second]).every((cell) => cell.plays.size === 2)).toBe(true);
});
test("film actions retain selected source and native contact, snap, entry and path clocks", () => {
  const select = vi.fn(); render(<RunEvidenceVisuals groups={groups()} position="RB" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect first contact at 1.20s" }));
  expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ play_id: "play", clip_id: "clip", tracklet_id: "rb", result_version: "job", focus_seconds: 1.2 }));
  fireEvent.click(screen.getByRole("button", { name: "Inspect original snap geometry" })); expect(select.mock.lastCall?.[0].focus_seconds).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: /Inspect actual LOS entry/ })); expect(select.mock.lastCall?.[0].focus_seconds).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Inspect path at 0.200s" })); expect(select.mock.lastCall?.[0].focus_seconds).toBe(.2);
  expect(screen.getByText("left A: 1 observed play")).toBeTruthy();
});
test("missing or contradictory contact and original front remain unavailable", () => {
  const input = one(); const packet = input[0].evidence[0].metric_packet!;
  packet.first_contact = { ...(packet.first_contact as object), actor_id: "other" }; packet.original_front = null;
  const run = observedRunEvidence(input)[0]; expect(run.contact).toBeNull(); expect(run.front).toBeNull(); expect(run.entry).toBeNull();
  render(<RunEvidenceVisuals groups={input} onSelect={vi.fn()} />);
  expect(screen.getByText("First contact unavailable")).toBeTruthy(); expect(screen.getByText("Original gap geometry unavailable")).toBeTruthy();
});
test("unknown shots, losses and discontinuities break paths; changed actor is refused", () => {
  const input = one(); const packet = input[0].evidence[0].metric_packet!;
  const path = packet.run_path as { trajectory: Record<string, unknown>[] };
  path.trajectory[2].visibility_state = "lost"; path.trajectory[4].shot_id = "unverified";
  expect(observedRunEvidence(input)[0].points.filter((p) => p === null).length).toBeGreaterThanOrEqual(2);
  expect(observedRunEvidence(input)[0].partial).toBe(true);
  path.trajectory.forEach((p) => { p.shot_id = "unknown"; }); expect(observedRunEvidence(input)).toEqual([]);
  packet.track_id = "different"; expect(observedRunEvidence(input)).toEqual([]);
});
test("claimed complete path without matching terminal evidence is only a prefix", () => {
  const input = one(); const path = input[0].evidence[0].metric_packet!.run_path as { event_evidence: { event_type: string }[] };
  path.event_evidence = path.event_evidence.filter((e) => e.event_type !== "end_of_play");
  expect(observedRunEvidence(input)[0].partial).toBe(true);
});
test("empty and other-position surfaces do not imply a zero-contact rate", () => {
  const view = render(<RunEvidenceVisuals groups={[]} position="RB" onSelect={vi.fn()} />);
  expect(screen.getByText(/Qualified carrier paths are unavailable/)).toBeTruthy();
  view.rerender(<RunEvidenceVisuals groups={groups()} position="WR" onSelect={vi.fn()} />); expect(view.container.childElementCount).toBe(0);
});
test("frame clock conflicts, predictions and interpolated points never count as native density visits", () => {
  const input = one(); const path = input[0].evidence[0].metric_packet!.run_path as { trajectory: Record<string, unknown>[] };
  path.trajectory[1].frame_number = path.trajectory[0].frame_number;
  path.trajectory[2].predicted = true; path.trajectory[3].interpolated = true;
  const run = observedRunEvidence(input)[0]; expect(run.partial).toBe(true);
  expect(run.points.filter((p) => p !== null).length).toBe(path.trajectory.length - 3);
});
test("contradictory gap names and mixed definitions cannot make gap or density counts", () => {
  const input = one(); (input[0].evidence[0].metric_packet!.entry as { gap_label: string }).gap_label = "right_A";
  const run = observedRunEvidence(input)[0]; expect(run.entry?.gap).toBeNull();
  expect(runDensity([run, { ...run, definition: "other" }])).toEqual([]);
});
test("actual producer path-only prefix renders without inventing contact or a completed outcome", () => {
  const r = fixture.prefix_rows[0];
  const evidence = { ...one()[0].evidence[0], metric_packet: structuredClone(r.metric_value) };
  const products = [{ metric_name: r.metric_name, unit: r.unit, definition_version: r.metric_value.definition_version, evidence: [evidence] }];
  render(<RunEvidenceVisuals groups={[]} products={products} position="RB" onSelect={vi.fn()} />);
  expect(screen.getByText(/Partial observed prefix/)).toBeTruthy();
  expect(screen.getByText("First contact unavailable")).toBeTruthy();
  expect(screen.getByText("Actual LOS entry unavailable")).toBeTruthy();
});
test("inward TE cannot supply a claimed edge inside the actual original tackle", () => {
  const input = one(); const packet = input[0].evidence[0].metric_packet!;
  const path = packet.run_path as { trajectory: { field_y: number }[] };
  path.trajectory.forEach((p) => { p.field_y = 3.5; });
  packet.first_contact = null;
  packet.entry = { timestamp_seconds: 1, field_y: 3.5, gap_label: "left_edge" };
  const front = packet.original_front as { roles: Record<string, unknown> };
  front.roles.LTE = { track_id: "te", field_x: 50, field_y: 3 };
  expect(observedRunEvidence(input)[0].entry?.gap).toBeNull();
});
