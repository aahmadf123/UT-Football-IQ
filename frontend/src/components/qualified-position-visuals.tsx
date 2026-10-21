"use client";

import { useMemo, useState } from "react";
import type { AnalyticsGroup, FilmEvidence, MetricEvidence, SpatialProduct } from "@/lib/analytics-api";
import { EChart } from "@/components/charts/echart";
import { FieldDiagram } from "@/components/field-diagram";
import { canonicalShotId } from "@/lib/shot-id";

type EvidenceRow = MetricEvidence & {
  player_id?: string | null; tracklet_id?: string | null; result_version?: string | null;
  right_censored?: boolean; subject?: string; metric_packet?: Record<string, unknown>;
};
export interface QBPair { timing: EvidenceRow; depth: EvidenceRow; definitionVersion: string }
export interface MeasuredPoint { seconds: number; value: number; shot?: string | number | null }
export interface FieldPoint extends MeasuredPoint { x: number; y: number }
type Series = { row: EvidenceRow; points: (MeasuredPoint | null)[]; name: string; definitionVersion?: string };
type FieldPath = { row: FilmEvidence; points: (FieldPoint | null)[]; definitionVersion?: string };
const WIDTH = 160 / 3;
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** Pair only measurements from the same actual athlete trajectory and completed result. */
export function qualifiedQBPairs(groups: AnalyticsGroup[]): QBPair[] {
  const timings = new Map<string, { row: EvidenceRow; definition: string }>();
  const depths = new Map<string, EvidenceRow>();
  const key = (row: EvidenceRow, version?: string) => row.player_id && row.tracklet_id && row.result_version && version
    ? JSON.stringify([row.play_id, row.clip_id, row.player_id, row.tracklet_id, row.result_version, version]) : null;
  for (const group of groups) {
    if (!(group.metric_name === "time_to_throw" && group.unit === "s") && !(group.metric_name === "dropback_depth" && group.unit === "yd")) continue;
    for (const raw of group.evidence) {
      const row: EvidenceRow = raw;
      const identity = key(row, group.definition_version);
      if (!identity || !finite(row.value) || row.right_censored || (group.metric_name === "time_to_throw" && row.value < 0)) continue;
      if (group.metric_name === "time_to_throw") timings.set(identity, { row, definition: group.definition_version! });
      else depths.set(identity, row);
    }
  }
  return [...timings].flatMap(([key, timing]) => {
    const depth = depths.get(key);
    return depth ? [{ timing: timing.row, depth, definitionVersion: timing.definition }] : [];
  });
}

/** Nulls preserve missing/lost/shot boundaries; no chart invents a continuous interval. */
function actualSeries(values: unknown, field: boolean): (FieldPoint | MeasuredPoint | null)[] {
  if (!Array.isArray(values)) return [];
  const output: (FieldPoint | MeasuredPoint | null)[] = [];
  let previous: MeasuredPoint | null = null;
  for (const entry of values) {
    if (!entry || typeof entry !== "object") { output.push(null); previous = null; continue; }
    const point = entry as Record<string, unknown>;
    const seconds = point.timestamp_seconds;
    const value = field ? point.field_x : point.width_yards;
    const shot = canonicalShotId(point.shot_id);
    const visible = !["lost", "occluded", "offscreen", "unobserved"].includes(String(point.visibility_state ?? ""));
    if (!visible || shot === null || !finite(seconds) || seconds < 0 || !finite(value)
        || (!field && value < 0) || (field && (!finite(point.field_y) || value < -10 || value > 110 || Math.abs(point.field_y) > WIDTH / 2))) {
      output.push(null); previous = null; continue;
    }
    if (previous && (seconds <= previous.seconds || seconds - previous.seconds > .2 + 1e-9 || shot !== previous.shot)) output.push(null);
    const current: FieldPoint | MeasuredPoint = field ? { seconds, value, shot, x: value, y: point.field_y as number } : { seconds, value, shot };
    output.push(current); previous = current;
  }
  return output;
}

export function qualifiedSpacingSeries(groups: AnalyticsGroup[]): Series[] {
  return groups.filter((group) => ["geometric_spacing", "pre_snap_split", "clear_lane_width"].includes(group.metric_name) && group.unit === "yd")
    .flatMap((group) => group.evidence.flatMap((raw) => {
      const row: EvidenceRow = raw;
      if (!row.result_version || row.right_censored) return [];
      const packet = row.metric_packet;
      const points = actualSeries(packet?.series ?? packet?.width_series, false) as (MeasuredPoint | null)[];
      return points.some(Boolean) ? [{ row, points, definitionVersion: group.definition_version, name: `${group.metric_name.replaceAll("_", " ")} · ${row.subject ?? "measured pair"}` }] : [];
    }));
}

export function qualifiedFieldPaths(groups: AnalyticsGroup[], products: SpatialProduct[] = []): FieldPath[] {
  const seen = new Set<string>();
  return [...groups, ...products].filter((group) => ["route_depth", "distance_traveled", "run_path", "observed_rush_path", "observed_pursuit_path", "observed_route_depth", "observed_pass_set_geometry", "lineman_first_second_displacement", "observed_yac"].includes(group.metric_name))
    .flatMap((group) => group.evidence.flatMap((raw) => {
      const row: FilmEvidence = raw;
      if (!row.result_version || !row.tracklet_id || row.right_censored) return [];
      const points = actualSeries(row.metric_packet?.trajectory, true) as (FieldPoint | null)[];
      const key = JSON.stringify([row.play_id, row.clip_id, row.tracklet_id, row.result_version]);
      if (!points.some(Boolean) || seen.has(key)) return [];
      seen.add(key);
      return [{ row, points, definitionVersion: group.definition_version }];
    }));
}

function segments(points: (FieldPoint | null)[]): FieldPoint[][] {
  const output: FieldPoint[][] = [];
  let current: FieldPoint[] = [];
  for (const point of points) {
    if (point) current.push(point);
    else if (current.length) { output.push(current); current = []; }
  }
  if (current.length) output.push(current);
  return output;
}

/** These charts consume the qualified cohort only; absent series stay absent. */
export function QualifiedPositionVisuals({ groups, products = [], position = "", onSelect }: {
  groups: AnalyticsGroup[]; products?: SpatialProduct[]; position?: string; onSelect: (row: FilmEvidence) => void;
}) {
  const qb = useMemo(() => qualifiedQBPairs(groups), [groups]);
  const spacing = useMemo(() => qualifiedSpacingSeries(groups), [groups]);
  const paths = useMemo(() => qualifiedFieldPaths(groups, products), [groups, products]);
  const [spacingId, setSpacingId] = useState("");
  const activeSpacing = spacing.find((series) => series.row.metric_id === spacingId) ?? spacing[0];
  const showQB = (!position || position === "QB") && qb.length > 0;
  const showOL = (!position || position === "OL") && spacing.length > 0;
  const showPaths = (!position || ["RB", "WR", "TE", "OL", "DB", "LB", "DL", "EDGE", "ST"].includes(position)) && paths.length > 0;
  const qbOption = useMemo(() => ({
    animation: false, grid: { left: 58, right: 24, bottom: 48, top: 24 },
    xAxis: { type: "value", name: "Time to throw (s)", nameLocation: "middle", nameGap: 28 },
    yAxis: { type: "value", name: "Dropback depth (yd)", scale: true },
    tooltip: { trigger: "item" },
    series: [{ type: "scatter", symbolSize: 10, itemStyle: { color: "#e4bb50" }, data: qb.map((pair) => [pair.timing.value, pair.depth.value]) }],
  }), [qb]);
  const spacingOption = useMemo(() => ({
    animation: false, grid: { left: 52, right: 24, bottom: 48, top: 24 },
    xAxis: { type: "value", name: "Source time (s)", nameLocation: "middle", nameGap: 28 },
    yAxis: { type: "value", name: "Width (yd)", scale: true }, tooltip: { trigger: "item" },
    series: activeSpacing ? [{ type: "line", name: activeSpacing.name, showSymbol: true, symbolSize: 5, connectNulls: false,
      data: activeSpacing.points.map((point) => point ? [point.seconds, point.value] : null) }] : [],
  }), [activeSpacing]);
  if (!showQB && !showOL && !showPaths) return null;
  return <section aria-label="Qualified position comparisons" className="grid gap-6 xl:grid-cols-2">
    {showQB ? <section aria-label="Quarterback release and dropback comparison" className="min-w-0 border-t border-border-soft pt-4">
      <h2 className="text-base font-semibold">Release timing and dropback depth</h2>
      <p className="mt-1 text-xs text-muted-foreground">{qb.length} eligible athlete-play pairs from the same trajectory and result. Definition {Array.from(new Set(qb.map((pair) => pair.definitionVersion))).join(", ")}. Positive depth means retreat from the snap position; negative depth means advancement. Unknown or unpaired measurements are excluded.</p>
      <EChart option={qbOption} label={`Quarterback time to throw versus dropback depth, ${qb.length} matched samples. Accessible values follow.`} onSelect={(index) => { if (qb[index]) onSelect(qb[index].timing); }} />
      <details><summary className="cursor-pointer text-sm">Measured pairs and film evidence</summary><div className="max-h-64 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><caption className="sr-only">Matched quarterback measurements</caption><thead><tr><th className="p-2">Sample</th><th className="p-2">Release (s)</th><th className="p-2">Depth (yd)</th><th className="p-2">Evidence</th></tr></thead><tbody>{qb.map((pair, index) => <tr key={`${pair.timing.metric_id}-${pair.depth.metric_id}`}><td className="p-2">{index + 1}</td><td className="p-2">{pair.timing.value.toFixed(2)}</td><td className="p-2">{pair.depth.value.toFixed(2)}</td><td className="p-2"><button type="button" onClick={() => onSelect(pair.timing)} className="text-primary underline">Inspect play</button></td></tr>)}</tbody></table></div></details>
    </section> : null}
    {showOL ? <section aria-label="Measured line spacing timelines" className="min-w-0 border-t border-border-soft pt-4">
      <h2 className="text-base font-semibold">Measured line spacing through time</h2>
      <p className="mt-1 text-xs text-muted-foreground">{spacing.length} eligible measured series in calibrated yards. Definition {activeSpacing?.definitionVersion ?? "unavailable"}. Lines stop at missing observations, shot changes and gaps above 200 ms.</p>
      <label className="mt-3 flex flex-col gap-1 text-xs text-muted-foreground">Measured spacing series<select className="rounded border border-border-soft bg-background p-2" value={activeSpacing?.row.metric_id ?? ""} onChange={(event) => setSpacingId(event.target.value)}>{spacing.map((series, index) => <option key={series.row.metric_id} value={series.row.metric_id}>{index + 1} · {series.name}</option>)}</select></label>
      <EChart option={spacingOption} label={`${activeSpacing?.name ?? "Measured spacing"}; source seconds and yards. Accessible observations follow.`} onSelect={() => { if (activeSpacing) onSelect(activeSpacing.row); }} />
      <details><summary className="cursor-pointer text-sm">Observations and film evidence</summary><div className="max-h-64 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><thead><tr><th className="p-2">Series</th><th className="p-2">Source (s)</th><th className="p-2">Width (yd)</th><th className="p-2">Evidence</th></tr></thead><tbody>{spacing.flatMap((series) => series.points.flatMap((point, index) => point ? [<tr key={`${series.row.metric_id}-${index}`}><td className="p-2">{series.name}</td><td className="p-2">{point.seconds.toFixed(2)}</td><td className="p-2">{point.value.toFixed(2)}</td><td className="p-2"><button type="button" onClick={() => onSelect({ ...series.row, focus_seconds: point.seconds })} className="text-primary underline">Inspect play</button></td></tr>] : []))}</tbody></table></div></details>
    </section> : null}
    {showPaths ? <section aria-label="Observed calibrated field paths" className="min-w-0 border-t border-border-soft pt-4">
      <h2 className="text-base font-semibold">Observed field paths</h2><p className="mt-1 text-xs text-muted-foreground">{paths.length} eligible trajectories, in canonical field yards. Definition {Array.from(new Set(paths.map((path) => path.definitionVersion ?? "unavailable"))).join(", ")}. Select a path to open its film evidence. Gaps and shot changes remain disconnected. Partial paths show only the observed interval and are excluded from complete scalar summaries.</p>
      <div className="relative mt-3"><FieldDiagram /><svg className="absolute inset-0 h-full w-full" viewBox="0 0 120 53.3" aria-label="Measured trajectories on an NCAA field" role="group">{paths.map((path, index) => <g key={path.row.metric_id} stroke={index % 2 ? "#38bdf8" : "#ffd20a"} fill="none" role="button" tabIndex={0} aria-label={`Inspect observed path ${index + 1}`} onClick={() => onSelect(path.row)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(path.row); } }}>{segments(path.points).map((segment, part) => <polyline key={part} points={segment.map((point) => `${point.x + 10},${(point.y + WIDTH / 2) / WIDTH * 53.3}`).join(" ")} strokeWidth=".35" />)}{path.points.filter((point): point is FieldPoint => point !== null).map((point, pointIndex) => <circle key={pointIndex} cx={point.x + 10} cy={(point.y + WIDTH / 2) / WIDTH * 53.3} r=".35" fill={index % 2 ? "#38bdf8" : "#ffd20a"} />)}</g>)}</svg></div>
      <details className="mt-3"><summary className="cursor-pointer text-sm">Coordinate observations and film evidence</summary><div className="max-h-64 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><thead><tr><th className="p-2">Path</th><th className="p-2">Source (s)</th><th className="p-2">Field x/y (yd)</th><th className="p-2">Evidence</th></tr></thead><tbody>{paths.flatMap((path, pathIndex) => path.points.flatMap((point, index) => point ? [<tr key={`${path.row.metric_id}-${index}`}><td className="p-2">{pathIndex + 1}</td><td className="p-2">{point.seconds.toFixed(2)}</td><td className="p-2">{point.x.toFixed(2)}, {point.y.toFixed(2)}</td><td className="p-2"><button type="button" onClick={() => onSelect({ ...path.row, focus_seconds: point.seconds })} className="text-primary underline">Inspect path</button></td></tr>] : []))}</tbody></table></div></details>
    </section> : null}
  </section>;
}
