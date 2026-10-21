import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AnalyticsGroup, SpatialProduct } from "@/lib/analytics-api";
import { MovementPositionVisuals, OpportunityEvidence, landingLocations, movementCurves } from "./movement-position-visuals";

vi.mock("@/components/charts/echart", () => ({ EChart: ({ label, onSelect }: { label: string; onSelect?: (index: number) => void }) => <button onClick={() => onSelect?.(0)}>{label}</button> }));
afterEach(cleanup);
const row = { play_id: "play", clip_id: "clip", metric_id: "distance", value: 2, video_id: "video", start_seconds: 10, end_seconds: 20, player_id: "wr", tracklet_id: "track", result_version: "run" };
const point = { timestamp_seconds: 10, yards: 2, shot_id: "a", actor_id: "track", opponent_id: "db", visibility_state: "observed" };
const group = (series: unknown[]): AnalyticsGroup => ({ key: "separation", metric_name: "observed_separation", unit: "yd", n: 1, mean: 2, median: 2, p10: 2, p90: 2, min: 2, max: 2, low_sample: true, definition_version: "v1", evidence: [{ ...row, metric_packet: { series, comparison_scope: "nearest_observed_defender" } }] });

test("separation curves retain actors, source clocks and observed breaks", () => {
  const curves = movementCurves([group([point, { ...point, timestamp_seconds: 10.1 }, { ...point, timestamp_seconds: 10.2, opponent_id: "other" }, { ...point, timestamp_seconds: 10.3, visibility_state: "lost" }, { ...point, timestamp_seconds: 10.4, shot_id: null }, { ...point, timestamp_seconds: 22 }])]);
  expect(curves[0].points.map((p) => p?.seconds ?? null)).toEqual([10, 10.1, null, 10.2, null, null, null]);
  expect(movementCurves([group([{ ...point, actor_id: "wrong" }])])).toEqual([]);
  expect(movementCurves([{ ...group([point]), definition_version: undefined }])).toEqual([]);
});

test("observed pair selections seek to their actual source instant", () => {
  const select = vi.fn();
  render(<MovementPositionVisuals groups={[group([point])]} position="WR" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect pair at 10.00s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ play_id: "play", focus_seconds: 10 }));
  expect(screen.getByText(/nearest observed defender/)).toBeTruthy();
});

const landing: SpatialProduct = { metric_name: "kick_landing_location", unit: "field_location", definition_version: "v1", evidence: [{ ...row, metric_packet: { field_x: 35, field_y: -12, timestamp_seconds: 13, visibility_state: "observed", landing_event_id: "landing", event_evidence: [{ event_id: "landing", event_type: "landing", timestamp_seconds: 13, shot_id: "a", source: "human" }] } }] };
test("landing maps preserve negative sideline coordinates and exclude unobserved or airborne positions", () => {
  expect(landingLocations([landing])[0]).toMatchObject({ x: 35, y: -12, seconds: 13 });
  const bad = { ...landing, evidence: [{ ...row, metric_packet: { ...landing.evidence[0].metric_packet, visibility_state: "lost" } }] };
  expect(landingLocations([bad])).toEqual([]);
  const select = vi.fn();
  render(<MovementPositionVisuals groups={[]} products={[landing]} position="ST" onSelect={select} />);
  fireEvent.keyDown(screen.getAllByRole("button", { name: "Inspect landing 1" })[0], { key: "Enter" });
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ focus_seconds: 13 }));
  expect(screen.getByText("35.00, -12.00")).toBeTruthy();
});

test("opportunity evidence works without an outcome and absent participation stays unknown", () => {
  const select = vi.fn();
  const view = render(<OpportunityEvidence opportunities={[]} onSelect={select} />);
  expect(screen.getByText(/opportunity coverage is unavailable/)).toBeTruthy();
  view.rerender(<OpportunityEvidence opportunities={[{ kind: "routes", definition_version: "v1", eligible_count: 2, player_count: 1, evidence: [row] }]} onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect routes rep 1" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ focus_seconds: 10 }));
  expect(screen.getByRole("cell", { name: "2" })).toBeTruthy();
});

test("no qualifying movement evidence produces no chart", () => {
  const { container } = render(<MovementPositionVisuals groups={[]} onSelect={vi.fn()} />);
  expect(container.childElementCount).toBe(0);
});


test("partial separation remains measured evidence without a scalar cohort", () => {
  const source = group([point]);
  const products: SpatialProduct[] = [{ metric_name: source.metric_name, definition_version: "v1", unit: "yd", evidence: [{ ...source.evidence[0], metric_packet: { ...source.evidence[0].metric_packet, partial_observation: true, measurement_complete: false } }] }];
  expect(movementCurves([], products)[0].partial).toBe(true);
  render(<MovementPositionVisuals groups={[]} products={products} position="WR" onSelect={vi.fn()} />);
  expect(screen.getByText(/Partial observed interval/)).toBeTruthy();
});

test("an opaque landing ID without matching clock and source evidence is unavailable", () => {
  for (const event_evidence of [[], [{ event_id: "landing", event_type: "catch", timestamp_seconds: 13, shot_id: "a", source: "human" }], [{ event_id: "landing", event_type: "landing", timestamp_seconds: 12, shot_id: "a", source: "human" }]]) {
    expect(landingLocations([{ ...landing, evidence: [{ ...landing.evidence[0], metric_packet: { ...landing.evidence[0].metric_packet, event_evidence } }] }])).toEqual([]);
  }
});


test("QB rusher distances use explicit rusher observations without declaring pressure", () => {
  const source = group([{ timestamp_seconds: 10, yards: 3, rusher_id: "edge", visibility_state: "observed", shot_id: "a" }]);
  source.metric_name = "rusher_proximity";
  source.evidence[0].metric_packet!.comparison_scope = "observed_resolved_rushers_only";
  source.evidence[0].metric_packet!.track_id = "track";
  expect(movementCurves([source])[0].points[0]?.opponent).toBe("edge");
  render(<MovementPositionVisuals groups={[source]} position="QB" onSelect={vi.fn()} />);
  expect(screen.getByText(/Distance alone does not establish pressure/)).toBeTruthy();
});


test("QB proximity cannot attach a different quarterback packet to this row", () => {
  const source = group([{ timestamp_seconds: 10, yards: 3, rusher_id: "edge", visibility_state: "observed", shot_id: "a" }]);
  source.metric_name = "rusher_proximity";
  source.evidence[0].metric_packet!.track_id = "other-qb";
  expect(movementCurves([source])).toEqual([]);
});

test("unknown shot placeholders stay unavailable and numeric zero shares its canonical source shot", () => {
  for (const shot_id of ["unresolved", "missing", "n/a"]) {
    expect(movementCurves([group([{ ...point, shot_id }])])).toEqual([]);
  }
  const curve = movementCurves([group([{ ...point, shot_id: 0 }, { ...point, timestamp_seconds: 10.1, shot_id: "00" }])])[0];
  expect(curve.points.map((p) => p?.shot)).toEqual(["0", "0"]);
  const broken = movementCurves([group([point, { ...point, timestamp_seconds: 10.05, shot_id: "unresolved" }, { ...point, timestamp_seconds: 10.1 }])])[0];
  expect(broken.points.map((p) => p?.seconds ?? null)).toEqual([10, null, 10.1]);
});
