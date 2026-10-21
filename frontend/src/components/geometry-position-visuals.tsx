"use client";

import { useMemo, useState } from "react";
import type { AnalyticsGroup, FilmEvidence, SpatialProduct } from "@/lib/analytics-api";
import { EChart } from "@/components/charts/echart";
import { FieldDiagram } from "@/components/field-diagram";
import { canonicalShotId } from "@/lib/shot-id";

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const observed = (value: unknown) => ["observed", "visible"].includes(String(value));
const inField = (x: unknown, y: unknown): boolean => finite(x) && x >= -10 && x <= 110 && finite(y) && Math.abs(y) <= 160 / 6;
const yScreen = (y: number) => (y + 160 / 6) / (160 / 3) * 53.3;
type Vertex = { x: number; y: number };
type Sample = { breakBefore: boolean; seconds: number; shot: unknown; polygon: Vertex[]; area: number; width: number; depth: number };
type Envelope = { row: FilmEvidence; definition: string; samples: Sample[]; partial: boolean };
function sourceEvent(packet: Record<string, unknown>, id: unknown, type: string, seconds: number, shot?: unknown): boolean {
  return typeof id === "string" && id.length > 0 && Array.isArray(packet.event_evidence) && packet.event_evidence.some((raw) => {
    const event = object(raw);
    const sourceShot = canonicalShotId(event?.shot_id);
    return event && event.event_id === id && event.event_type === type && event.timestamp_seconds === seconds
      && ["human", "manual", "real", "model"].includes(String(event.source)) && sourceShot !== null
      && (shot === undefined || canonicalShotId(shot) === sourceShot);
  });
}

/** No hulls or projected player boxes are substituted for a measured envelope. */
export function measuredEnvelopes(groups: AnalyticsGroup[], products: SpatialProduct[] = []): Envelope[] {
  const seen = new Set<string>();
  return [...groups, ...products].filter((group) => group.metric_name.startsWith("protection_envelope_") && group.definition_version)
    .flatMap((group) => group.evidence.flatMap((row) => {
      const packet = row.metric_packet;
      if (!row.result_version || !packet || packet.definition_basis !== "explicit_measured_protection_polygon" || !Array.isArray(packet.series)) return [];
      const key = JSON.stringify([row.play_id, row.clip_id, row.result_version, group.definition_version, packet.envelope_id ?? packet.subject_id ?? "unspecified"]);
      if (seen.has(key)) return [];
      let barrier = false;
      const reject = () => { barrier = true; return []; };
      const samples = packet.series.flatMap((raw) => {
        const point = object(raw);
        const shot = canonicalShotId(point?.shot_id);
        if (!point || !finite(point.timestamp_seconds) || point.timestamp_seconds < row.start_seconds || point.timestamp_seconds > row.end_seconds
          || shot === null || !observed(point.visibility_state) || !Array.isArray(point.polygon) || point.polygon.length < 3
          || !sourceEvent(packet, point.event_id, "protection_envelope", point.timestamp_seconds, shot)) return reject();
        const polygon = point.polygon.map(object).flatMap((v) => v && inField(v.field_x, v.field_y) ? [{ x: v.field_x as number, y: v.field_y as number }] : []);
        if (polygon.length !== point.polygon.length) return reject();
        const area = Math.abs(polygon.reduce((sum, v, index) => { const next = polygon[(index + 1) % polygon.length]; return sum + v.x * next.y - next.x * v.y; }, 0)) / 2;
        const width = Math.max(...polygon.map((v) => v.y)) - Math.min(...polygon.map((v) => v.y));
        const depth = Math.max(...polygon.map((v) => v.x)) - Math.min(...polygon.map((v) => v.x));
        if (area <= 0 || !finite(point.area_square_yards) || Math.abs(area - point.area_square_yards) > .001
          || !finite(point.width_yards) || Math.abs(width - point.width_yards) > .001 || !finite(point.depth_yards) || Math.abs(depth - point.depth_yards) > .001) return reject();
        const breakBefore = barrier;
        barrier = false;
        return [{ breakBefore, seconds: point.timestamp_seconds, shot, polygon, area, width, depth }];
      });
      if (!samples.length) return [];
      seen.add(key);
      return [{ row, definition: group.definition_version!, samples, partial: packet.partial_observation === true }];
    }));
}

export function receiverLocations(products: SpatialProduct[]) {
  const types: Record<string, string> = { receiver_release_location: "receiver_release", receiver_break_location: "route_break", observed_cut_location: "carrier_cut" };
  return products.filter((product) => types[product.metric_name] && product.unit === "field_location" && product.definition_version)
    .flatMap((product) => product.evidence.flatMap((row) => {
      const packet = row.metric_packet;
      if (!packet || !row.tracklet_id || !row.result_version || packet.actor_id !== row.tracklet_id || row.right_censored
        || !inField(packet.field_x, packet.field_y) || !observed(packet.visibility_state) || !finite(packet.timestamp_seconds)
        || packet.timestamp_seconds < row.start_seconds || packet.timestamp_seconds > row.end_seconds
        || !sourceEvent(packet, packet.location_event_id, types[product.metric_name], packet.timestamp_seconds)) return [];
      return [{ row, x: packet.field_x as number, y: packet.field_y as number, seconds: packet.timestamp_seconds,
        name: types[product.metric_name].replaceAll("_", " "), definition: product.definition_version }];
    }));
}

export function GeometryPositionVisuals({ groups, products = [], position = "", onSelect }: {
  groups: AnalyticsGroup[]; products?: SpatialProduct[]; position?: string; onSelect: (row: FilmEvidence) => void;
}) {
  const envelopes = useMemo(() => !position || ["QB", "OL"].includes(position) ? measuredEnvelopes(groups, products) : [], [groups, products, position]);
  const locations = useMemo(() => !position || ["RB", "WR", "TE"].includes(position) ? receiverLocations(products) : [], [products, position]);
  const [envelopeId, setEnvelopeId] = useState("");
  const [sampleIndex, setSampleIndex] = useState(0);
  const active = envelopes.find((item) => item.row.metric_id === envelopeId) ?? envelopes[0];
  const sample = active?.samples[Math.min(sampleIndex, active.samples.length - 1)];
  const plot = useMemo(() => active?.samples.flatMap((point, index) => {
    const previous = active.samples[index - 1];
    return previous && (point.breakBefore || point.seconds <= previous.seconds || point.seconds - previous.seconds > .2 + 1e-9 || point.shot !== previous.shot) ? [null, point] : [point];
  }) ?? [], [active]);
  const option = useMemo(() => ({ animation: false, grid: { left: 55, right: 24, top: 24, bottom: 48 },
    xAxis: { type: "value", name: "Source time (s)", nameLocation: "middle", nameGap: 28 }, yAxis: { type: "value", name: "Area (yd²)", scale: true }, tooltip: { trigger: "item" },
    series: [{ type: "line", connectNulls: false, showSymbol: true, symbolSize: 6, data: plot.map((point) => point ? [point.seconds, point.area] : null) }],
  }), [plot]);
  if (!active && !locations.length) return null;
  return <section aria-label="Measured specialist geometry" className="grid gap-6 xl:grid-cols-2">
    {active && sample ? <section aria-label="Measured protection envelope" className="min-w-0 border-t border-border-soft pt-4"><h2 className="text-base font-semibold">Measured protection envelope</h2><p className="mt-1 text-xs text-muted-foreground">{active.partial ? "Partial measured interval; excluded from complete scalar summaries. " : ""}Definition {active.definition}. Polygons are explicit field measurements; geometry does not establish protection success.</p>
      <label className="mt-3 flex flex-col gap-1 text-xs text-muted-foreground">Measured envelope play<select className="rounded border border-border-soft bg-background p-2" value={active.row.metric_id} onChange={(event) => { setEnvelopeId(event.target.value); setSampleIndex(0); }}>{envelopes.map((item, index) => <option key={item.row.metric_id} value={item.row.metric_id}>{index + 1} · {item.row.play_id.slice(0, 8)}</option>)}</select></label>
      <EChart option={option} label="Measured protection envelope area through source time, in square yards. Source observations follow." onSelect={(index) => { const point = plot[index]; if (point) { setSampleIndex(active.samples.indexOf(point)); onSelect({ ...active.row, focus_seconds: point.seconds }); } }} />
      {active.samples.length > 1 ? <label className="flex flex-col gap-1 text-xs text-muted-foreground">Measured envelope sample<input type="range" min={0} max={active.samples.length - 1} step={1} value={Math.min(sampleIndex, active.samples.length - 1)} onChange={(event) => setSampleIndex(Number(event.target.value))} /></label> : null}
      <div className="relative mt-3"><FieldDiagram /><svg className="absolute inset-0 h-full w-full" viewBox="0 0 120 53.3" role="img" aria-label={`Measured polygon at ${sample.seconds.toFixed(2)} source seconds`}><polygon points={sample.polygon.map((v) => `${v.x + 10},${yScreen(v.y)}`).join(" ")} stroke="#e4bb50" strokeWidth=".3" fill="#e4bb50" fillOpacity=".2" /></svg></div>
      <p className="mt-2 text-sm tabular-nums">Source {sample.seconds.toFixed(2)}s · width {sample.width.toFixed(2)} yd · depth {sample.depth.toFixed(2)} yd · area {sample.area.toFixed(2)} yd²</p><button className="mt-2 text-sm text-primary underline" type="button" onClick={() => onSelect({ ...active.row, focus_seconds: sample.seconds })}>Inspect envelope at {sample.seconds.toFixed(2)}s</button>
    </section> : null}
    {locations.length ? <section aria-label="Observed player event locations" className="min-w-0 border-t border-border-soft pt-4"><h2 className="text-base font-semibold">Player event locations</h2><p className="mt-1 text-xs text-muted-foreground">Actor-linked, observed event locations in canonical field yards. No route family or coverage assignment is inferred.</p><div className="relative mt-3"><FieldDiagram /><svg className="absolute inset-0 h-full w-full" viewBox="0 0 120 53.3" role="img" aria-label="Player event field locations">{locations.map((point) => <circle key={point.row.metric_id} cx={point.x + 10} cy={yScreen(point.y)} r=".7" fill="#38bdf8" />)}</svg></div><table className="mt-3 w-full text-left text-xs tabular-nums"><thead><tr><th className="p-2">Event</th><th className="p-2">Field x/y (yd)</th><th className="p-2">Definition</th><th className="p-2">Film</th></tr></thead><tbody>{locations.map((point) => <tr key={point.row.metric_id}><td className="p-2">{point.name}</td><td className="p-2">{point.x.toFixed(2)}, {point.y.toFixed(2)}</td><td className="p-2">{point.definition}</td><td className="p-2"><button className="text-primary underline" type="button" onClick={() => onSelect({ ...point.row, focus_seconds: point.seconds })}>Inspect {point.name} at {point.seconds.toFixed(2)}s</button></td></tr>)}</tbody></table></section> : null}
  </section>;
}
