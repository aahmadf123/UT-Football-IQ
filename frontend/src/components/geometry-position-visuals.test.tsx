import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SpatialProduct } from "@/lib/analytics-api";
import { GeometryPositionVisuals, measuredEnvelopes, receiverLocations } from "./geometry-position-visuals";

vi.mock("@/components/charts/echart", () => ({ EChart: ({ label }: { label: string }) => <div>{label}</div> }));
afterEach(cleanup);
const row = { play_id: "play", clip_id: "clip", metric_id: "geometry", video_id: "video", start_seconds: 10, end_seconds: 20, tracklet_id: "actor", result_version: "run" };
const polygon = [{ field_x: 40, field_y: -3 }, { field_x: 45, field_y: -3 }, { field_x: 45, field_y: 3 }, { field_x: 40, field_y: 3 }];
const event = { event_id: "measured", event_type: "protection_envelope", source: "human", timestamp_seconds: 11, shot_id: "a" };
const sample = { ...event, polygon, width_yards: 6, depth_yards: 5, area_square_yards: 30, visibility_state: "observed" };
const envelope: SpatialProduct = { metric_name: "protection_envelope_area", unit: "yd²", definition_version: "v1", evidence: [{ ...row, metric_packet: { definition_basis: "explicit_measured_protection_polygon", partial_observation: true, series: [sample], event_evidence: [event] } }] };

test("envelopes use exact measured polygons and source event evidence", () => {
  expect(measuredEnvelopes([], [envelope])[0].samples[0]).toMatchObject({ seconds: 11, area: 30 });
  for (const packet of [{ ...envelope.evidence[0].metric_packet, event_evidence: [] }, { ...envelope.evidence[0].metric_packet, series: [{ ...sample, shot_id: "other" }] }]) {
    expect(measuredEnvelopes([], [{ ...envelope, evidence: [{ ...row, metric_packet: packet }] }])).toEqual([]);
  }
  const select = vi.fn();
  render(<GeometryPositionVisuals groups={[]} products={[envelope]} position="QB" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect envelope at 11.00s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ focus_seconds: 11 }));
  expect(screen.getByText(/Partial measured interval/)).toBeTruthy();
});

test("receiver points require the actor-bound event at the matching source clock", () => {
  const product: SpatialProduct = { metric_name: "receiver_break_location", unit: "field_location", definition_version: "v1", evidence: [{ ...row, metric_packet: { actor_id: "actor", location_event_id: "break", timestamp_seconds: 12, field_x: 55, field_y: -8, visibility_state: "observed", event_evidence: [{ event_id: "break", event_type: "route_break", source: "human", timestamp_seconds: 12, shot_id: "a" }] } }] };
  expect(receiverLocations([product])[0]).toMatchObject({ x: 55, y: -8 });
  expect(receiverLocations([{ ...product, evidence: [{ ...row, metric_packet: { ...product.evidence[0].metric_packet, actor_id: "other" } }] }])).toEqual([]);
  const select = vi.fn();
  render(<GeometryPositionVisuals groups={[]} products={[product]} position="WR" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect route break at 12.00s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ focus_seconds: 12 }));
});

test("carrier cut map uses the actual cut event and seeks its source instant", () => {
  const product: SpatialProduct = { metric_name: "observed_cut_location", unit: "field_location", definition_version: "v1", evidence: [{ ...row, metric_packet: { actor_id: "actor", location_event_id: "cut", cut_event_id: "cut", timestamp_seconds: 13, field_x: 52, field_y: 3, visibility_state: "observed", event_evidence: [{ event_id: "cut", event_type: "carrier_cut", source: "human", timestamp_seconds: 13, shot_id: "a" }] } }] };
  const select = vi.fn();
  render(<GeometryPositionVisuals groups={[]} products={[product]} position="RB" onSelect={select} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect carrier cut at 13.00s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ focus_seconds: 13, tracklet_id: "actor" }));
  expect(receiverLocations([{ ...product, evidence: [{ ...row, metric_packet: { ...product.evidence[0].metric_packet, location_event_id: "foreign" } }] }])).toEqual([]);
});


test("invalid envelope samples remain a break before the next measured polygon", () => {
  const later = { ...sample, event_id: "later", timestamp_seconds: 11.1 };
  const packet = { ...envelope.evidence[0].metric_packet, series: [sample, { ...sample, timestamp_seconds: 11.05, visibility_state: "lost" }, later], event_evidence: [event, { ...event, event_id: "later", timestamp_seconds: 11.1 }] };
  const result = measuredEnvelopes([], [{ ...envelope, evidence: [{ ...row, metric_packet: packet }] }]);
  expect(result[0].samples[1].breakBefore).toBe(true);
});


test("envelope subjects stay distinct while dimensions of the same subject share one view", () => {
  const subject = (id: string, name = "protection_envelope_area"): SpatialProduct => ({ ...envelope, metric_name: name, evidence: [{ ...envelope.evidence[0], metric_id: `${id}-${name}`, metric_packet: { ...envelope.evidence[0].metric_packet, envelope_id: id } }] });
  expect(measuredEnvelopes([], [subject("one"), subject("one", "protection_envelope_width"), subject("two")])).toHaveLength(2);
});

test("polygon source matching rejects unknown shots and normalizes numeric shot zero", () => {
  const product = (shot: unknown, sourceShot: unknown = shot): SpatialProduct => ({ ...envelope, evidence: [{ ...row, metric_packet: {
    ...envelope.evidence[0].metric_packet, series: [{ ...sample, shot_id: shot }], event_evidence: [{ ...event, shot_id: sourceShot }],
  } }] });
  for (const shot of ["unresolved", "missing", "n/a"]) expect(measuredEnvelopes([], [product(shot)])).toEqual([]);
  expect(measuredEnvelopes([], [product(0, "00")])[0].samples[0].shot).toBe("0");
});
