import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SpatialProduct } from "@/lib/analytics-api";
import fixture from "./__fixtures__/original-front-producer.json";
import { OriginalFrontVisuals, originalFronts } from "./original-front-visuals";

const charts = vi.hoisted(() => ({ draw: vi.fn() }));
vi.mock("@/components/charts/echart", () => ({ EChart: (props: { label: string; option: unknown }) => { charts.draw(props.option); return <div role="img" aria-label={props.label} />; } }));
afterEach(() => { cleanup(); charts.draw.mockClear(); });

function product(): SpatialProduct {
  return {
    metric_name: "lineman_first_second_displacement", definition_version: "observed-geometry-contract", unit: "yd",
    evidence: structuredClone(fixture.rows).map((row, index) => ({
      metric_id: `metric-${index}`, play_id: "play", clip_id: "clip", video_id: "video",
      tracklet_id: row.tracklet_id, player_id: `player-${index}`, result_version: "job",
      start_seconds: 0, end_seconds: 2, metric_packet: row.metric_value,
    })),
  };
}
const series = (p: SpatialProduct, index = 0) => p.evidence[index].metric_packet!.series as Record<string, unknown>[];

test("actual specialist producer packets retain five named actors and signed observations", () => {
  expect(fixture.fixture_kind).toBe("synthetic_producer_contract_only");
  const fronts = originalFronts([], [product()]);
  expect(fronts).toHaveLength(1);
  expect(Object.keys(fronts[0].actors)).toEqual(["LT", "LG", "C", "RG", "RT"]);
  expect(fronts[0].clocks).toHaveLength(11);
  expect(fronts[0].actors.LT?.points.at(-1)?.forward).toBe(-1);
  expect(fronts[0].actors.LT?.points.at(-1)?.frame).toBe(10);
});

test("mounted source control uses an exact sample and opens film at that recording clock", () => {
  const select = vi.fn();
  render(<OriginalFrontVisuals groups={[]} products={[product()]} position="OL" onSelect={select} />);
  expect(screen.getAllByRole("img", { name: /observed forward and lateral/ })).toHaveLength(5);
  fireEvent.change(screen.getByRole("slider", { name: "Trench source sample" }), { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "Inspect film at 0.700s" }));
  expect(select).toHaveBeenCalledWith(expect.objectContaining({ clip_id: "clip", focus_seconds: .7, result_version: "job" }));
});

test("a partial actor is not frozen at its last observed position", () => {
  const p = product();
  p.evidence[0].metric_packet!.series = series(p).slice(0, 5);
  p.evidence[0].metric_packet!.source_end_seconds = .4;
  p.evidence[0].metric_packet!.partial_observation = true;
  p.evidence[0].metric_packet!.measurement_complete = false;
  render(<OriginalFrontVisuals groups={[]} products={[p]} onSelect={vi.fn()} />);
  fireEvent.change(screen.getByRole("slider"), { target: { value: "10" } });
  const row = screen.getByRole("row", { name: /LT Unobserved at this source frame Unknown/ });
  expect(within(row).getByText("Unobserved at this source frame")).toBeTruthy();
  expect(screen.getByRole("row", { name: /RT 49.00, -4.00 -1.00, 0.00/ })).toBeTruthy();
});

test("a missing original role remains explicitly unobserved", () => {
  const p = product(); p.evidence = p.evidence.slice(1);
  render(<OriginalFrontVisuals groups={[]} products={[p]} onSelect={vi.fn()} />);
  expect(screen.getByRole("option", { name: /4\/5 named trajectories/ })).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "LT observed displacement" })).getByText("Unobserved")).toBeTruthy();
});

test.each(["clip_id", "result_version", "video_id"] as const)("%s is never joined into another measured front", (field) => {
  const p = product(); p.evidence[0][field] = "other";
  const fronts = originalFronts([], [p]);
  expect(fronts).toHaveLength(2);
  expect(fronts.map((front) => Object.keys(front.actors).length).sort()).toEqual([1, 4]);
});

test("competing actors for one original role invalidate that front", () => {
  const p = product(); const duplicate = structuredClone(p.evidence[0]);
  duplicate.metric_id = "competing"; duplicate.tracklet_id = "other-track";
  duplicate.metric_packet!.actor_id = duplicate.metric_packet!.track_id = duplicate.tracklet_id;
  p.evidence.push(duplicate);
  expect(originalFronts([], [p])).toEqual([]);
});

test("identical evidence copies do not create extra actors or fronts", () => {
  const p = product(); p.evidence.push(structuredClone(p.evidence[0]));
  expect(Object.keys(originalFronts([], [p])[0].actors)).toHaveLength(5);
});

test("conflicting duplicate samples and contradictory displacement values are refused", () => {
  for (const conflict of ["duplicate", "signed_value"]) {
    const p = product();
    if (conflict === "duplicate") series(p).splice(2, 0, structuredClone(series(p)[1]));
    else series(p)[3].forward_yards = 40;
    expect(originalFronts([], [p])).toEqual([]);
  }
});

test("a source timestamp cannot identify different native frames across actors", () => {
  const p = product(); series(p)[5].frame_number = 50;
  expect(originalFronts([], [p])).toEqual([]);
});

test("a native frame cannot identify different source timestamps across actors", () => {
  const p = product(); series(p)[5].timestamp_seconds = .51;
  expect(originalFronts([], [p])).toEqual([]);
});

test("out-of-order source observations are refused rather than sorted into a path", () => {
  const p = product(); const values = series(p);
  [values[4], values[5]] = [values[5], values[4]];
  expect(originalFronts([], [p])).toEqual([]);
});

test("unknown shot and lost samples remain gaps in the charts and source view", () => {
  const p = product(); series(p)[4].shot_id = "unknown"; series(p)[5].visibility_state = "lost";
  render(<OriginalFrontVisuals groups={[]} products={[p]} onSelect={vi.fn()} />);
  const options = charts.draw.mock.calls.map((call) => call[0] as { series: { connectNulls: boolean; data: unknown[] }[] });
  expect(options[0].series[0].connectNulls).toBe(false);
  expect(options[0].series[0].data.filter((point) => point === null)).toHaveLength(2);
  fireEvent.change(screen.getByRole("slider"), { target: { value: "4" } });
  expect(screen.getByRole("row", { name: /LT Unobserved at this source frame/ })).toBeTruthy();
});

test("foreign actor packets do not supply a named trajectory", () => {
  const p = product(); p.evidence[0].metric_packet!.actor_id = "foreign";
  expect(originalFronts([], [p])[0].actors.LT).toBeUndefined();
});

test("other positions and unsupported units do not render a line comparison", () => {
  const p = product(); p.unit = "m";
  expect(originalFronts([], [p])).toEqual([]);
  render(<OriginalFrontVisuals groups={[]} products={[product()]} position="WR" onSelect={vi.fn()} />);
  expect(screen.queryByRole("region", { name: "Original offensive line comparisons" })).toBeNull();
});
