import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AnalyticsGroup } from "@/lib/analytics-api";
import { SpecialistPositionVisuals, eventTimelines, releaseLocations, gapObservations } from "./specialist-position-visuals";

vi.mock("@/components/charts/echart", () => ({ EChart: ({ label, onSelect }: { label: string; onSelect?: (index: number) => void }) => <button onClick={() => onSelect?.(0)}>{label}</button> }));
afterEach(cleanup);
const row = { play_id: "play", clip_id: "clip", metric_id: "metric", video_id: "video", value: 2, start_seconds: 10, end_seconds: 20, tracklet_id: "qb-track", result_version: "run" };
const group = (name: string, packet: Record<string, unknown>): AnalyticsGroup => ({ key: name, metric_name: name, unit: name === "clear_lane_width" ? "yd" : "s", n: 1, mean: 2, median: 2, p10: 2, p90: 2, min: 2, max: 2, low_sample: true, definition_version: "v1", eligible_count: 1, unknown_count: 3, evidence: [{ ...row, metric_packet: packet }] });
const events = [{ event_id: "snap", event_type: "snap", timestamp_seconds: 12, source: "staff" }, { event_id: "release", event_type: "throw", timestamp_seconds: 14, source: "staff" }];

test("event timelines use actual source events and exact seek context", () => {
  const groups = [group("time_to_throw", { event_evidence: events })];
  expect(eventTimelines(groups)[0].events).toHaveLength(2);
  const select = vi.fn();
  render(<SpecialistPositionVisuals groups={groups} position="QB" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect throw at 14.00 seconds" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ metric_id: "metric", focus_seconds: 14 }));
  expect(screen.getByText(/3 unknown/)).toBeTruthy();
});

test("a release map requires an actual release event and a fresh visible same-view point", () => {
  const packet = { event_evidence: events, trajectory: [{ timestamp_seconds: 14, field_x: 48, field_y: -12, shot_id: "a", visibility_state: "visible" }] };
  expect(releaseLocations([group("time_to_throw", packet)])).toHaveLength(1);
  for (const change of [{ event_evidence: [] }, { trajectory: [{ ...packet.trajectory[0], timestamp_seconds: 13.5 }] }, { trajectory: [{ ...packet.trajectory[0], visibility_state: "lost" }] }, { trajectory: [{ ...packet.trajectory[0], field_y: 80 }] }]) {
    expect(releaseLocations([group("time_to_throw", { ...packet, ...change })])).toHaveLength(0);
  }
  expect(releaseLocations([group("time_to_throw", { ...packet, trajectory: [
    { ...packet.trajectory[0], timestamp_seconds: 13.95 },
    { timestamp_seconds: 13.99, shot_id: "a", visibility_state: "lost" },
  ] })])).toHaveLength(0);
  expect(releaseLocations([group("time_to_throw", { ...packet, event_evidence: [{ ...events[1], event_type: "pass_release" }] })])).toHaveLength(1);
  expect(releaseLocations([group("time_to_throw", { ...packet, event_evidence: [{ ...events[1], event_type: "forward_pass_release" }] })])).toHaveLength(1);
  expect(releaseLocations([group("time_to_throw", { ...packet, trajectory: [
    { ...packet.trajectory[0], timestamp_seconds: 13.95 },
    { ...packet.trajectory[0], timestamp_seconds: 13.99, visibility_state: "unobserved" },
  ] })])).toHaveLength(0);
});

test("gap heatmap preserves distinct initial gap subjects and measured timestamps", () => {
  const packet = { gap_label: "Left A", subject_id: "C-LG", measurement_region: { cross_section: "LOS", los_x: 50, y_min: -3, y_max: 0 }, series: [{ timestamp_seconds: 12, width_yards: 1.25 }, { timestamp_seconds: 12.1, width_yards: 0, visibility_state: "visible" }, { timestamp_seconds: 12.2, width_yards: 4, visibility_state: "occluded" }] };
  const result = gapObservations([group("clear_lane_width", packet)]);
  expect(result).toHaveLength(2);
  expect(result[1].width).toBe(0);
  expect(gapObservations([group("clear_lane_width", { ...packet, measurement_region: undefined })])).toHaveLength(0);
  const select = vi.fn();
  render(<SpecialistPositionVisuals groups={[group("clear_lane_width", packet)]} position="OL" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: /Gap width heatmap/ }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ focus_seconds: 12 }));
  expect(screen.getByText(/clear section does not establish/)).toBeTruthy();
});

test("unsupported or absent evidence never creates decorative specialist charts", () => {
  const { container } = render(<SpecialistPositionVisuals groups={[group("time_to_throw", {})]} position="QB" onSelect={vi.fn()} />);
  expect(container.childElementCount).toBe(0);
  expect(eventTimelines([group("time_to_throw", { event_evidence: [{ ...events[0], timestamp_seconds: 9 }] })])).toHaveLength(0);
});
