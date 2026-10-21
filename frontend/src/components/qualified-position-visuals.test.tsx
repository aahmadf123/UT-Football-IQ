import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AnalyticsGroup } from "@/lib/analytics-api";
import { QualifiedPositionVisuals, qualifiedFieldPaths, qualifiedQBPairs, qualifiedSpacingSeries } from "./qualified-position-visuals";

vi.mock("@/components/charts/echart", () => ({ EChart: ({ label, onSelect }: { label: string; onSelect?: (index: number) => void }) => <button onClick={() => onSelect?.(0)}>{label}</button> }));
afterEach(cleanup);
const row = { play_id: "play", clip_id: "clip", metric_id: "ttt", value: 2.5, video_id: "video", start_seconds: 0, end_seconds: 10, player_id: "qb", tracklet_id: "track", result_version: "run" };
const group = (name: string, unit: string, value = row.value, extra = {}): AnalyticsGroup => ({ key: name, metric_name: name, unit, n: 1, mean: value, median: value, p10: value, p90: value, min: value, max: value, low_sample: true, definition_version: "v1", evidence: [{ ...row, metric_id: name, value, ...extra }] });

test("QB scatter pairs only the same canonical athlete trajectory and result", () => {
  const time = group("time_to_throw", "s");
  expect(qualifiedQBPairs([time, group("dropback_depth", "yd", 6)])).toHaveLength(1);
  for (const mismatch of [{ result_version: "other" }, { tracklet_id: "other" }, { player_id: "other" }, { player_id: null }, { result_version: undefined }, { right_censored: true }, { value: Number.NaN }]) {
    expect(qualifiedQBPairs([time, group("dropback_depth", "yd", 6, mismatch)])).toHaveLength(0);
  }
  expect(qualifiedQBPairs([time, { ...group("dropback_depth", "yd", 6), definition_version: "v2" }])).toHaveLength(0);
});

test("missing qualified measurements produce no decorative charts", () => {
  const { container } = render(<QualifiedPositionVisuals groups={[group("time_to_throw", "s")]} position="QB" onSelect={vi.fn()} />);
  expect(container.childElementCount).toBe(0);
});

test("signed dropback accepts advancement while release duration stays nonnegative", () => {
  const signedDepth = group("dropback_depth", "yd", -2);
  const pairs = qualifiedQBPairs([group("time_to_throw", "s", 2), signedDepth]);
  expect(pairs).toHaveLength(1);
  expect(pairs[0].depth.value).toBe(-2);
  expect(qualifiedQBPairs([group("time_to_throw", "s", -1), signedDepth])).toHaveLength(0);
  render(<QualifiedPositionVisuals groups={[group("time_to_throw", "s", 2), signedDepth]} position="QB" onSelect={vi.fn()} />);
  expect(screen.getByText("-2.00")).toBeTruthy();
});

test("the paired QB chart and accessible table open the actual contributing row", () => {
  const select = vi.fn();
  render(<QualifiedPositionVisuals groups={[group("time_to_throw", "s"), group("dropback_depth", "yd", 6)]} position="QB" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: /Quarterback time to throw/ }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ metric_id: "time_to_throw", value: 2.5 }));
  expect(screen.getByText("6.00")).toBeTruthy();
});

test("spacing charts use actual samples and break at lost observations or long gaps", () => {
  const series = [{ timestamp_seconds: 0, width_yards: 2, shot_id: "a" }, { timestamp_seconds: .1, visibility_state: "lost" }, { timestamp_seconds: .2, width_yards: 3, shot_id: "a" }, { timestamp_seconds: 2, width_yards: 4, shot_id: "a" }];
  const result = qualifiedSpacingSeries([group("geometric_spacing", "yd", 2, { metric_packet: { series } })]);
  expect(result[0].points).toEqual([{ seconds: 0, value: 2, shot: "a" }, null, { seconds: .2, value: 3, shot: "a" }, null, { seconds: 2, value: 4, shot: "a" }]);
  expect(qualifiedSpacingSeries([group("geometric_spacing", "yd", 2)])).toHaveLength(0);
});

test("field paths keep centered negative coordinates and never connect a shot cut", () => {
  const trajectory = [{ timestamp_seconds: 0, field_x: 50, field_y: -10, shot_id: "a" }, { timestamp_seconds: .1, field_x: 51, field_y: 2, shot_id: "b" }];
  const groups = [group("route_depth", "yd", 2, { metric_packet: { trajectory } }), group("distance_traveled", "yd", 2, { metric_packet: { trajectory } })];
  const result = qualifiedFieldPaths(groups);
  expect(result).toHaveLength(1);
  expect(result[0].points[0]).toMatchObject({ x: 50, y: -10 });
  expect(result[0].points[1]).toBeNull();
  const select = vi.fn();
  render(<QualifiedPositionVisuals groups={groups} position="WR" onSelect={select} />);
  fireEvent.keyDown(screen.getByRole("button", { name: "Inspect observed path 1" }), { key: "Enter" });
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ metric_id: "route_depth" }));
});

test("measured paths and spacing stop at unknown shot markers and preserve shot zero", () => {
  const series = [{ timestamp_seconds: 0, width_yards: 2, shot_id: 0 }, { timestamp_seconds: .05, width_yards: 9, shot_id: "unresolved" }, { timestamp_seconds: .1, width_yards: 3, shot_id: "00" }];
  const spacing = qualifiedSpacingSeries([group("geometric_spacing", "yd", 2, { metric_packet: { series } })]);
  expect(spacing[0].points.map((p) => p ? [p.seconds, p.shot] : null)).toEqual([[0, "0"], null, [.1, "0"]]);
  for (const shot_id of ["unresolved", "missing", "n/a"]) {
    expect(qualifiedFieldPaths([group("route_depth", "yd", 2, { metric_packet: { trajectory: [{ timestamp_seconds: 0, field_x: 50, field_y: 0, shot_id }] } })])).toEqual([]);
  }
});
