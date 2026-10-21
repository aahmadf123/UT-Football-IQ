import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AnalyticsGroup } from "@/lib/analytics-api";
import { InteractionPositionVisuals, qualifiedInteractions } from "./interaction-position-visuals";
import { qualifiedTrenchReplays } from "./trench-interaction-replay";
import fixture from "./__fixtures__/trench-interaction-producer.json";

afterEach(cleanup);
const group: AnalyticsGroup = { key: "OL", metric_name: "engagement_duration", unit: "s", definition_version: "contract", n: 1, mean: .3, median: .3, p10: .3, p90: .3, min: .3, max: .3, low_sample: true, evidence: [{
  play_id: "play", video_id: "video", clip_id: "clip", metric_id: "interaction", value: .3, start_seconds: 0, end_seconds: 1,
  tracklet_id: "ol", result_version: "result", metric_packet: fixture.metric.metric_value,
}] };
const parsed = (packet: Record<string, unknown>) => qualifiedTrenchReplays(qualifiedInteractions([{ ...group, evidence: [{ ...group.evidence[0], metric_packet: packet }] }]));

test("actual producer packet renders exact paired frames and opens the source instant", () => {
  const select = vi.fn();
  render(<InteractionPositionVisuals groups={[group]} position="OL" onSelect={select} />);
  expect(screen.getByRole("img", { name: "Paired trench positions at native frame 0" }).querySelectorAll("circle")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Next observed frame" }));
  expect(screen.getByText("Native frame 1 · 0.100s")).toBeTruthy();
  expect(screen.getByRole("img", { name: "Paired trench positions at native frame 1" }).querySelectorAll("circle")).toHaveLength(1);
  expect(screen.getByText("Unobserved")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Inspect paired film at 0.100s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ clip_id: "clip", focus_seconds: .1 }));
  fireEvent.change(screen.getByRole("slider", { name: "Interaction source frame" }), { target: { value: "3" } });
  expect(screen.getByText("Native frame 3 · 0.300s")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Previous observed frame" }));
  expect(screen.getByText("Native frame 2 · 0.200s")).toBeTruthy();
});

test("replay requires its own calibrated pair, exact ordered clocks and known shot", () => {
  const packet = group.evidence[0].metric_packet!;
  const geometry = fixture.metric.metric_value.interaction_geometry;
  for (const changes of [{ calibration_safe: false }, { clock_basis: "fps" }, { blocker_id: "foreign" }, { defender_id: "foreign" }, { shot_id: "unresolved" }, { shot_id: "other" }, { source_end_seconds: 9 }, { series_interpolation: "linear" },
    { samples: [geometry.samples[0], geometry.samples[0]] },
    { samples: [geometry.samples[1], geometry.samples[0]] },
    { samples: [{ ...geometry.samples[0], timestamp_seconds: -1 }, geometry.samples[1]] },
    { samples: [{ ...geometry.samples[0], blocker: { field_x: 999, field_y: 0 } }, geometry.samples[1]] }]) {
    expect(parsed({ ...packet, interaction_geometry: { ...geometry, ...changes } })).toEqual([]);
  }
  render(<InteractionPositionVisuals groups={[{ ...group, evidence: [{ ...group.evidence[0], metric_packet: { ...packet, interaction_geometry: null } }] }]} onSelect={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Inspect interaction at 0.00s" })).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Observed trench interaction replay" })).toBeNull();
});

test("conflicting measurements for the same pair do not choose the first report", () => {
  const other = { ...group.evidence[0], metric_id: "conflict", metric_packet: { ...group.evidence[0].metric_packet, source_end_seconds: .2,
    event_evidence: [fixture.metric.metric_value.event_evidence[0], { ...fixture.metric.metric_value.event_evidence[1], timestamp_seconds: .2 }],
  }, value: .2 };
  expect(qualifiedInteractions([{ ...group, evidence: [group.evidence[0], other] }])).toEqual([]);
  expect(qualifiedInteractions([{ ...group, evidence: [other, group.evidence[0]] }])).toEqual([]);
});

test("traces stop across absent actors and source gaps", () => {
  const geometry = fixture.metric.metric_value.interaction_geometry;
  const sample = { ...geometry.samples[3], timestamp_seconds: .6, frame_number: 6 };
  const packet = { ...group.evidence[0].metric_packet, source_end_seconds: .6, event_evidence: [fixture.metric.metric_value.event_evidence[0], { ...fixture.metric.metric_value.event_evidence[1], timestamp_seconds: .6 }], interaction_geometry: { ...geometry, source_end_seconds: .6, samples: [...geometry.samples, sample] } };
  render(<InteractionPositionVisuals groups={[{ ...group, evidence: [{ ...group.evidence[0], value: .6, metric_packet: packet }] }]} onSelect={vi.fn()} />);
  fireEvent.change(screen.getByRole("slider", { name: "Interaction source frame" }), { target: { value: "4" } });
  expect(screen.getByRole("status").textContent).toContain("Observation gap");
  const paths = screen.getByRole("img", { name: "Paired trench positions at native frame 6" }).querySelectorAll("polyline");
  expect(paths).toHaveLength(5); // Two blocker sections, three defender sections.
});
