"use client";

import { useMemo } from "react";
import type { AnalyticsGroup, FilmEvidence, SpatialProduct } from "@/lib/analytics-api";
import { canonicalShotId } from "@/lib/shot-id";
import { EChart } from "@/components/charts/echart";

type Sample = { seconds: number; width: number; eventId: string; shot: string };
type Bound = { lower: number | null; upper: number | null; censored: boolean };
type Aligned = { kind: string; seconds: number; width: number; row: FilmEvidence };
type Lane = { row: FilmEvidence; label: string; subject: string; definition: string; threshold: number;
  los: number; low: number; high: number; snap: number; samples: Sample[]; openings: Bound[]; closures: Bound[]; aligned: Aligned[] };
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;

function sourceEvents(packet: Record<string, unknown>, row: FilmEvidence) {
  return Array.isArray(packet.event_evidence) ? packet.event_evidence.flatMap((raw) => {
    const event = object(raw), shot = canonicalShotId(event?.shot_id);
    if (!event || !shot || typeof event.event_id !== "string" || !event.event_id || !finite(event.timestamp_seconds)
      || event.timestamp_seconds < row.start_seconds || event.timestamp_seconds > row.end_seconds
      || !["human", "manual", "real", "model"].includes(String(event.source))) return [];
    return [{ id: event.event_id, type: event.event_type, seconds: event.timestamp_seconds, shot }];
  }) : [];
}

/** Widths and interval endpoints must match actual supplied measurement clocks. */
export function qualifiedLaneWindows(groups: AnalyticsGroup[], products: SpatialProduct[] = []): Lane[] {
  const seen = new Set<string>();
  return products.filter((product) => product.metric_name === "gap_clear_timeline" && product.unit === "source_interval" && product.definition_version)
    .flatMap((product) => product.evidence.flatMap((row) => {
      const packet = row.metric_packet, region = object(packet?.measurement_region);
      if (!row.result_version || !packet || packet.occupancy_complete !== true || packet.series_interpolation !== "none"
        || packet.continuous_openness_claimed !== false || packet.threshold_operator !== ">"
        || !finite(packet.opening_threshold_yards) || packet.opening_threshold_yards < 0
        || !finite(packet.snap_seconds) || typeof packet.gap_label !== "string" || typeof packet.subject_id !== "string"
        || !region || region.cross_section !== "LOS" || !finite(region.los_x) || region.los_x < 0 || region.los_x > 100
        || !finite(region.y_min) || !finite(region.y_max) || region.y_min >= region.y_max
        || region.y_min < -80 / 3 || region.y_max > 80 / 3 || !Array.isArray(packet.series)) return [];
      const events = sourceEvents(packet, row);
      const snap = events.filter((event) => event.type === "snap" && event.seconds === packet.snap_seconds);
      if (snap.length !== 1) return [];
      const clocks = packet.series.map((raw) => object(raw)?.timestamp_seconds);
      if (clocks.some((when, index) => !finite(when) || (index > 0 && (!finite(clocks[index - 1]) || when <= (clocks[index - 1] as number))))) return [];
      const samples = packet.series.flatMap<Sample>((raw) => {
        const point = object(raw), shot = canonicalShotId(point?.shot_id);
        if (!point || !shot || shot !== snap[0].shot || point.visibility_state !== "observed" || !finite(point.timestamp_seconds)
          || !finite(point.width_yards) || point.width_yards < 0 || point.width_yards > (region.y_max as number) - (region.y_min as number) + 1e-8 || typeof point.event_id !== "string") return [];
        const matched = events.filter((event) => event.id === point.event_id && event.seconds === point.timestamp_seconds && event.shot === shot
          && ["snap", "geometry_observation"].includes(String(event.type)));
        return matched.length === 1 ? [{ seconds: point.timestamp_seconds, width: point.width_yards, eventId: point.event_id, shot }] : [];
      });
      if (!samples.length || new Set(samples.map((sample) => sample.seconds)).size !== samples.length) return [];
      const bounds = (raw: unknown, opening: boolean): Bound[] => Array.isArray(raw) ? raw.flatMap<Bound>((value) => {
        const bound = object(value);
        if (!bound) return [];
        const lower = bound.lower_seconds, upper = bound.upper_seconds;
        if (!(lower === null || finite(lower)) || !(upper === null || finite(upper)) || (lower === null && upper === null)
          || (lower !== null && !samples.some((sample) => sample.seconds === lower))
          || (upper !== null && !samples.some((sample) => sample.seconds === upper))
          || (lower !== null && upper !== null && lower >= upper)
          || (opening ? bound.left_censored !== (lower === null) || upper === null : bound.right_censored !== (upper === null) || lower === null)) return [];
        if (packet.unknown_clock_observations !== 0) return [];
        const lowIndex = lower === null ? -1 : clocks.indexOf(lower), highIndex = upper === null ? -1 : clocks.indexOf(upper);
        const lowSample = samples.find((sample) => sample.seconds === lower), highSample = samples.find((sample) => sample.seconds === upper);
        const threshold = packet.opening_threshold_yards as number;
        if (lower !== null && upper !== null && highIndex !== lowIndex + 1) return [];
        if (opening && (!highSample || highSample.width <= threshold || (lowSample && lowSample.width > threshold))) return [];
        if (!opening && (!lowSample || lowSample.width <= threshold || (highSample && highSample.width > threshold))) return [];
        if (lower === null && highIndex > 0 && samples.some((sample) => sample.seconds === clocks[highIndex - 1])) return [];
        if (upper === null && lowIndex < clocks.length - 1 && samples.some((sample) => sample.seconds === clocks[lowIndex + 1])) return [];
        return [{ lower, upper, censored: opening ? lower === null : upper === null }];
      }) : [];
      const aligned = groups.filter((group) => ["clear_lane_width_at_handoff", "clear_lane_width_at_entry"].includes(group.metric_name)
        && group.unit === "yd" && group.definition_version === product.definition_version).flatMap((group) => group.evidence.flatMap<Aligned>((item) => {
          const value = item.metric_packet;
          if (!value || !item.tracklet_id || item.clip_id !== row.clip_id || item.play_id !== row.play_id || item.result_version !== row.result_version
            || value.subject_id !== packet.subject_id || value.carrier_id !== item.tracklet_id || !finite(value.alignment_seconds)
            || !finite(item.value) || value.width_yards !== item.value || !samples.some((sample) => sample.seconds === value.alignment_seconds
              && sample.width === item.value && sample.eventId === value.measurement_event_id)) return [];
          const source = sourceEvents(value, item);
          if (!source.some((event) => event.type === "handoff" && event.id === value.alignment_event_id && event.shot === snap[0].shot)) return [];
          const entry = object(value.actual_entry);
          const kind = group.metric_name.endsWith("_entry") ? "entry" : "handoff";
          if (kind === "handoff" && !source.some((event) => event.id === value.alignment_event_id && event.seconds === value.alignment_seconds)) return [];
          if (kind === "entry" && (!entry || entry.entry_seconds !== value.alignment_seconds || entry.gap_label !== packet.gap_label || entry.track_id !== item.tracklet_id)) return [];
          return [{ kind, seconds: value.alignment_seconds, width: item.value, row: item }];
        }));
      const key = JSON.stringify([row.play_id, row.clip_id, packet.subject_id, row.result_version, product.definition_version, packet.opening_threshold_yards]);
      if (seen.has(key)) return [];
      seen.add(key);
      return [{ row, label: packet.gap_label, subject: packet.subject_id, definition: product.definition_version,
        threshold: packet.opening_threshold_yards, los: region.los_x, low: region.y_min, high: region.y_max,
        snap: packet.snap_seconds, samples, openings: bounds(packet.openings, true), closures: bounds(packet.closures, false), aligned }];
    }));
}

function LaneCard({ lane, onSelect }: { lane: Lane; onSelect: (row: FilmEvidence) => void }) {
  const option = useMemo(() => ({ grid: { left: 55, right: 20, top: 25, bottom: 50 },
    xAxis: { type: "value", name: "Seconds from snap", nameLocation: "middle", nameGap: 30 },
    yAxis: { type: "value", name: "LOS width (yd)", min: 0 }, tooltip: { trigger: "item" },
    series: [{ type: "scatter", symbolSize: 9, data: lane.samples.map((sample) => [sample.seconds - lane.snap, sample.width]) }] }), [lane]);
  return <article className="min-w-0 rounded-lg border border-border-soft p-3"><h3 className="text-sm font-semibold">{lane.label.replaceAll("_", " ")} · LOS {lane.los.toFixed(2)} yd</h3><p className="mt-1 text-xs text-muted-foreground">Original section y={lane.low.toFixed(2)} to {lane.high.toFixed(2)} yd. Open means width &gt; {lane.threshold.toFixed(2)} yd at a measured section. Measurements are discrete; openness between samples is unknown.</p>
    <ul className="mt-3 space-y-1 text-xs">{lane.openings.map((bound, index) => <li key={`open-${index}`}>{bound.lower === null ? `Opening already present by ${bound.upper!.toFixed(2)}s (left censored)` : `Opening between ${bound.lower.toFixed(2)} and ${bound.upper!.toFixed(2)}s`}</li>)}{lane.closures.map((bound, index) => <li key={`close-${index}`}>{bound.upper === null ? `Closure unobserved after ${bound.lower!.toFixed(2)}s (right censored)` : `Closure between ${bound.lower!.toFixed(2)} and ${bound.upper.toFixed(2)}s`}</li>)}</ul>
    <div className="mt-3 flex flex-wrap gap-3">{lane.aligned.map((item) => <div key={`${item.kind}-${item.row.metric_id}`} className="rounded border border-border-soft p-2 text-xs"><strong className="block">{item.kind === "entry" ? "Actual entry" : "Handoff"} · {item.width.toFixed(2)} yd</strong><button className="mt-1 text-primary underline" type="button" onClick={() => onSelect({ ...item.row, focus_seconds: item.seconds })}>Inspect {item.kind === "entry" ? "entry" : "handoff"} width at {item.seconds.toFixed(2)}s</button></div>)}</div>
    <EChart option={option} label={`Discrete LOS clear width for ${lane.label}`} onSelect={(index) => { const sample = lane.samples[index]; if (sample) onSelect({ ...lane.row, focus_seconds: sample.seconds }); }} />
    <details><summary className="cursor-pointer text-xs">Measured samples and source definition</summary><p className="mt-2 break-all text-xs text-muted-foreground">Definition {lane.definition} · original gap {lane.subject} · no successful running path or blocking grade is implied.</p><table className="mt-2 w-full text-left text-xs tabular-nums"><thead><tr><th className="p-1">Source seconds</th><th className="p-1">Width (yd)</th><th className="p-1">Film</th></tr></thead><tbody>{lane.samples.map((sample) => <tr key={sample.eventId}><td className="p-1">{sample.seconds.toFixed(2)}</td><td className="p-1">{sample.width.toFixed(2)}</td><td className="p-1"><button type="button" className="text-primary underline" onClick={() => onSelect({ ...lane.row, focus_seconds: sample.seconds })}>Inspect measured section</button></td></tr>)}</tbody></table></details>
  </article>;
}

export function LaneWindowVisuals({ groups, products = [], position = "", onSelect }: {
  groups: AnalyticsGroup[]; products?: SpatialProduct[]; position?: string; onSelect: (row: FilmEvidence) => void;
}) {
  const lanes = useMemo(() => !position || ["OL", "RB", "TE"].includes(position) ? qualifiedLaneWindows(groups, products) : [], [groups, products, position]);
  if (!lanes.length) return null;
  return <section aria-label="Measured LOS lane windows" className="border-t border-border-soft pt-4"><h2 className="text-base font-semibold">Measured LOS lane windows</h2><p className="mt-1 text-xs text-muted-foreground">Change bounds and film links use recording seconds; the chart is aligned to snap. Handoff and entry widths appear only at an exact measured clock; unavailable widths are omitted.</p><div className="mt-4 grid gap-4 xl:grid-cols-2">{lanes.map((lane) => <LaneCard key={`${lane.row.metric_id}-${lane.subject}`} lane={lane} onSelect={onSelect} />)}</div></section>;
}
