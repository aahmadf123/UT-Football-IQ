import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AnalyticsGroup } from "@/lib/analytics-api";
import { InteractionPositionVisuals, qualifiedInteractions } from "./interaction-position-visuals";

afterEach(cleanup);
const row = { play_id: "play", clip_id: "clip", metric_id: "engagement", value: 1, video_id: "video", start_seconds: 10, end_seconds: 20, player_id: "blocker", tracklet_id: "blocker-track", result_version: "run", metric_packet: { track_id: "blocker-track", defender_id: "defender-track", interaction_id: "interaction", source_start_seconds: 11, source_end_seconds: 12, event_evidence: [{ event_id: "start", event_type: "engagement_start", timestamp_seconds: 11, source: "human", shot_id: "a" }, { event_id: "end", event_type: "engagement_end", timestamp_seconds: 12, source: "human", shot_id: "a" }] } };
const group: AnalyticsGroup = { key: "OL", metric_name: "engagement_duration", definition_version: "v1", n: 1, mean: 1, median: 1, p10: 1, p90: 1, min: 1, max: 1, unit: "s", low_sample: true, evidence: [row] };

test("interaction matrix is bound to explicit paired event clocks and opens film", () => {
  expect(qualifiedInteractions([group])).toHaveLength(1);
  const select = vi.fn();
  render(<InteractionPositionVisuals groups={[group]} position="OL" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect interaction at 11.00s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ focus_seconds: 11 }));
  expect(screen.getByText(/interaction observations/)).toBeTruthy();
});

test("distance, missing endpoint evidence and cross-shot intervals cannot create interactions", () => {
  for (const packet of [{ ...row.metric_packet, event_evidence: [] }, { ...row.metric_packet, track_id: "other" }, { ...row.metric_packet, event_evidence: [row.metric_packet.event_evidence[0], { ...row.metric_packet.event_evidence[1], shot_id: "b" }] }]) {
    expect(qualifiedInteractions([{ ...group, evidence: [{ ...row, metric_packet: packet }] }])).toEqual([]);
  }
  expect(qualifiedInteractions([{ ...group, metric_name: "rusher_proximity" }])).toEqual([]);
});

test("interaction endpoints use the same normalized known shot including zero", () => {
  const withShots = (first: unknown, second: unknown) => [{ ...group, evidence: [{ ...row, metric_packet: { ...row.metric_packet,
    event_evidence: [{ ...row.metric_packet.event_evidence[0], shot_id: first }, { ...row.metric_packet.event_evidence[1], shot_id: second }],
  } }] }];
  for (const shot of ["unresolved", "missing", "n/a"]) expect(qualifiedInteractions(withShots(shot, shot))).toEqual([]);
  expect(qualifiedInteractions(withShots(0, "00"))).toHaveLength(1);
});
