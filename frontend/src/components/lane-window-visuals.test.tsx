import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AnalyticsGroup, SpatialProduct } from "@/lib/analytics-api";
import fixture from "./__fixtures__/lane-window-producer.json";
import { LaneWindowVisuals, qualifiedLaneWindows } from "./lane-window-visuals";

vi.mock("@/components/charts/echart", () => ({ EChart: ({ label }: { label: string }) => <div>{label}</div> }));
afterEach(cleanup);
const base = { play_id: "play", clip_id: "clip", video_id: "video", start_seconds: 0, end_seconds: .4, result_version: "run", definition_version: "2026-10-21.1" };
const timeline = fixture.rows.find((row) => row.metric_name === "gap_clear_timeline")!;
const products: SpatialProduct[] = [{ metric_name: timeline.metric_name, unit: timeline.unit, definition_version: base.definition_version,
  evidence: [{ ...base, metric_id: "timeline", metric_packet: timeline.metric_value }] }];
const groups: AnalyticsGroup[] = fixture.rows.filter((row) => row.metric_name !== "gap_clear_timeline").map((row) => ({
  key: row.metric_name, metric_name: row.metric_name, definition_version: base.definition_version, unit: row.unit,
  n: 1, mean: 1.2, median: 1.2, p10: 1.2, p90: 1.2, min: 1.2, max: 1.2, low_sample: true,
  evidence: [{ ...base, metric_id: row.metric_name, tracklet_id: row.tracklet_id, value: row.metric_value.width_yards!, metric_packet: row.metric_value }],
}));

test("actual measured-body producer output renders interval clocks and exact event widths", () => {
  const lanes = qualifiedLaneWindows(groups, products);
  expect(lanes).toHaveLength(1);
  expect(lanes[0].samples).toHaveLength(5);
  const select = vi.fn();
  render(<LaneWindowVisuals groups={groups} products={products} position="OL" onSelect={select} />);
  expect(screen.getByText(/Opening between 0.00 and 0.10s/)).toBeTruthy();
  expect(screen.getByText(/Closure between 0.20 and 0.30s/)).toBeTruthy();
  expect(screen.getByText(/Closure unobserved after 0.40s/)).toBeTruthy();
  expect(screen.getByText(/Handoff · 1.20 yd/)).toBeTruthy();
  expect(screen.getByText(/Actual entry · 1.20 yd/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Inspect handoff width at 0.10s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ clip_id: "clip", focus_seconds: .1 }));
});

test("missing source evidence and foreign-clock event widths cannot display", () => {
  expect(qualifiedLaneWindows(groups, [{ ...products[0], evidence: [{ ...products[0].evidence[0], metric_packet: { ...timeline.metric_value, event_evidence: [] } }] }])).toEqual([]);
  const changed = groups.map((group) => ({ ...group, evidence: group.evidence.map((row) => ({ ...row, metric_packet: { ...row.metric_packet, alignment_seconds: .15 } })) }));
  expect(qualifiedLaneWindows(changed, products)[0].aligned).toEqual([]);
  const select = vi.fn();
  render(<LaneWindowVisuals groups={groups} products={products} position="DB" onSelect={select} />);
  expect(screen.queryByText("Measured LOS lane windows")).toBeNull();
});

test("opening and closure bounds require adjacent measured opposite threshold states", () => {
  const modified = (change: Record<string, unknown>) => [{ ...products[0], evidence: [{ ...products[0].evidence[0], metric_packet: { ...timeline.metric_value, ...change } }] }];
  expect(qualifiedLaneWindows(groups, modified({ openings: [{ lower_seconds: .1, upper_seconds: .2, left_censored: false }] }))[0].openings).toEqual([]);
  expect(qualifiedLaneWindows(groups, modified({ closures: [{ lower_seconds: 0, upper_seconds: .1, right_censored: false }] }))[0].closures).toEqual([]);
  expect(qualifiedLaneWindows(groups, modified({ openings: [{ lower_seconds: 0, upper_seconds: .4, left_censored: false }] }))[0].openings).toEqual([]);
  const series = timeline.metric_value.series.map((point, index) => index === 1 ? { ...point, width_yards: null, visibility_state: "unobserved" } : point);
  expect(qualifiedLaneWindows(groups, modified({ series, openings: [{ lower_seconds: 0, upper_seconds: .2, left_censored: false }] }))[0].openings).toEqual([]);
  expect(qualifiedLaneWindows(groups, modified({ series: [...timeline.metric_value.series].reverse() }))).toEqual([]);
});
