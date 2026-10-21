"use client";

import { useMemo, useState } from "react";
import type { AnalyticsGroup, FilmEvidence, PositionOpportunity, SpatialProduct } from "@/lib/analytics-api";
import { EChart } from "@/components/charts/echart";
import { FieldDiagram } from "@/components/field-diagram";
import { canonicalShotId } from "@/lib/shot-id";

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const visible = (value: unknown) => ["visible", "observed"].includes(String(value));
type Point = { seconds: number; value: number; shot: unknown; opponent: string };
type Curve = { row: FilmEvidence; name: string; unit: string; definition: string; scope: string; partial: boolean; points: (Point | null)[] };

/** Read native observations only; switching opponents, losses and cuts break lines. */
export function movementCurves(groups: AnalyticsGroup[], products: SpatialProduct[] = []): Curve[] {
  return [...groups, ...products].flatMap((group) => {
    const proximity = group.metric_name === "rusher_proximity";
    const key = ["observed_separation", "rusher_proximity"].includes(group.metric_name) && group.unit === "yd" ? "yards"
      : group.metric_name === "observed_closing_speed" && group.unit === "yd/s" ? "yards_per_second" : null;
    if (!key || !group.definition_version) return [];
    return group.evidence.flatMap((row) => {
      const packet = row.metric_packet;
      if (!row.tracklet_id || !row.result_version || row.right_censored || !Array.isArray(packet?.series)) return [];
      if (proximity && packet.track_id !== row.tracklet_id) return [];
      const points: (Point | null)[] = [];
      let previous: Point | null = null;
      for (const raw of packet.series) {
        const p = object(raw);
        const shot = canonicalShotId(p?.shot_id);
        if (!p || !finite(p.timestamp_seconds) || p.timestamp_seconds < row.start_seconds || p.timestamp_seconds > row.end_seconds
          || !finite(p[key]) || (key === "yards" && (p[key] as number) < 0) || shot === null || !visible(p.visibility_state)
          || typeof (proximity ? p.rusher_id : p.opponent_id) !== "string" || !(proximity ? p.rusher_id : p.opponent_id)
          || (!proximity && p.actor_id !== row.tracklet_id)) {
          points.push(null); previous = null; continue;
        }
        const point = { seconds: p.timestamp_seconds, value: p[key] as number, shot, opponent: (proximity ? p.rusher_id : p.opponent_id) as string };
        if (previous && (point.seconds <= previous.seconds || point.seconds - previous.seconds > .2 + 1e-9
          || point.shot !== previous.shot || point.opponent !== previous.opponent)) points.push(null);
        points.push(point); previous = point;
      }
      return points.some(Boolean) ? [{ row, points, name: group.metric_name.replaceAll("_", " "), unit: group.unit, partial: packet.partial_observation === true,
        definition: group.definition_version!, scope: String(packet.comparison_scope ?? "explicit comparison actor") }] : [];
    });
  });
}

export function landingLocations(products: SpatialProduct[]) {
  return products.filter((product) => product.metric_name === "kick_landing_location" && product.unit === "field_location" && product.definition_version)
    .flatMap((product) => product.evidence.flatMap((row) => {
      const packet = row.metric_packet;
      if (!row.tracklet_id || !row.result_version || !packet || !finite(packet.field_x) || packet.field_x < -10 || packet.field_x > 110
        || !finite(packet.field_y) || Math.abs(packet.field_y) > 160 / 6 || !finite(packet.timestamp_seconds)
        || packet.timestamp_seconds < row.start_seconds || packet.timestamp_seconds > row.end_seconds
        || row.right_censored || !visible(packet.visibility_state) || typeof packet.landing_event_id !== "string" || !packet.landing_event_id
        || !Array.isArray(packet.event_evidence) || !packet.event_evidence.some((raw) => {
          const event = object(raw);
          return event && event.event_id === packet.landing_event_id && event.event_type === "landing"
            && event.timestamp_seconds === packet.timestamp_seconds && canonicalShotId(event.shot_id) !== null
            && ["human", "manual", "real", "model"].includes(String(event.source));
        })) return [];
      return [{ row, x: packet.field_x, y: packet.field_y, seconds: packet.timestamp_seconds, definition: product.definition_version }];
    }));
}

export function OpportunityEvidence({ opportunities, onSelect }: { opportunities?: PositionOpportunity[]; onSelect: (row: FilmEvidence) => void }) {
  if (!opportunities?.length) return <p className="text-xs text-muted-foreground">Position opportunity coverage is unavailable for this cohort. Rates require reviewed participation evidence.</p>;
  return <section aria-label="Qualified position opportunities" className="border-t border-border-soft pt-4">
    <h2 className="text-base font-semibold">Eligible position opportunities</h2>
    <p className="mt-1 text-xs text-muted-foreground">Each resolved player and canonical play counts once per opportunity kind and definition, including reps without measured outcomes. These are observed opportunities; missing participation coverage remains unknown.</p>
    <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">Opportunity</th><th className="p-2">Eligible reps</th><th className="p-2">Players</th><th className="p-2">Definition</th><th className="p-2">Film</th></tr></thead><tbody>{opportunities.map((group) => <tr key={`${group.kind}-${group.definition_version}`} className="border-t border-border-soft"><td className="p-2">{group.kind.replaceAll("_", " ")}</td><td className="p-2 tabular-nums">{group.eligible_count}</td><td className="p-2 tabular-nums">{group.player_count}</td><td className="p-2 text-xs">{group.definition_version}</td><td className="p-2"><details><summary className="cursor-pointer">Inspect opportunities</summary><div className="mt-2 flex max-h-40 flex-col gap-2 overflow-auto">{group.evidence.map((row, index) => <button className="text-left text-primary underline" type="button" key={row.metric_id} onClick={() => onSelect({ ...row, focus_seconds: finite(row.metric_packet?.source_start_seconds) ? row.metric_packet.source_start_seconds : row.start_seconds })}>Inspect {group.kind.replaceAll("_", " ")} rep {index + 1}</button>)}</div></details></td></tr>)}</tbody></table></div>
  </section>;
}

export function MovementPositionVisuals({ groups, products = [], position = "", onSelect }: {
  groups: AnalyticsGroup[]; products?: SpatialProduct[]; position?: string; onSelect: (row: FilmEvidence) => void;
}) {
  const curves = useMemo(() => !position || ["QB", "WR", "TE", "DB", "LB", "DL", "EDGE"].includes(position) ? movementCurves(groups, products).filter((curve) => position !== "QB" || curve.name === "rusher proximity") : [], [groups, products, position]);
  const landings = useMemo(() => !position || position === "ST" ? landingLocations(products) : [], [products, position]);
  const [curveId, setCurveId] = useState("");
  const active = curves.find((curve) => curve.row.metric_id === curveId) ?? curves[0];
  const te = (!position || position === "TE") ? groups.filter((group) => group.metric_name === "te_usage_transition" && group.unit === "s" && group.definition_version).flatMap((group) => group.evidence.flatMap((row) => {
    if (!row.result_version || !row.tracklet_id || row.right_censored || !Array.isArray(row.metric_packet?.series)) return [];
    return row.metric_packet.series.flatMap((raw) => {
      const p = object(raw);
      return p && ["block", "chip", "route"].includes(String(p.state)) && p.actor_id === row.tracklet_id && canonicalShotId(p.shot_id) !== null
        && typeof p.event_id === "string" && visible(p.visibility_state) && finite(p.timestamp_seconds)
        && p.timestamp_seconds >= row.start_seconds && p.timestamp_seconds <= row.end_seconds
        ? [{ row, state: String(p.state), seconds: p.timestamp_seconds, definition: group.definition_version! }] : [];
    });
  })) : [];
  const option = useMemo(() => ({
    animation: false, grid: { left: 55, right: 24, top: 24, bottom: 48 },
    xAxis: { type: "value", name: "Source time (s)", nameLocation: "middle", nameGap: 28 },
    yAxis: { type: "value", name: active?.unit ?? "yd", scale: true }, tooltip: { trigger: "item" },
    series: active ? [{ type: "line", showSymbol: true, symbolSize: 5, connectNulls: false, data: active.points.map((point) => point ? [point.seconds, point.value] : null) }] : [],
  }), [active]);
  if (!curves.length && !landings.length && !te.length) return null;
  return <section aria-label="Observed movement and participation" className="grid gap-6 xl:grid-cols-2">
    {active ? <section className="min-w-0 border-t border-border-soft pt-4" aria-label="Separation and closing timelines"><h2 className="text-base font-semibold">Observed distances and closing through time</h2><p className="mt-1 text-xs text-muted-foreground">{active.scope.replaceAll("_", " ")} · definition {active.definition}. {active.partial ? "Partial observed interval; excluded from complete scalar summaries. " : ""}Positive closing speed means decreasing distance. Distance alone does not establish pressure or coverage responsibility. Comparison actors, missing observations, shot changes and gaps above 200 ms remain explicit.</p>
      <label className="mt-3 flex flex-col gap-1 text-xs text-muted-foreground">Observed movement series<select className="rounded border border-border-soft bg-background p-2" value={active.row.metric_id} onChange={(event) => setCurveId(event.target.value)}>{curves.map((curve, index) => <option key={curve.row.metric_id} value={curve.row.metric_id}>{index + 1} · {curve.name}</option>)}</select></label>
      <EChart option={option} label={`${active.name} in ${active.unit}; source-time observations and film evidence follow.`} onSelect={(index) => { const point = active.points[index]; if (point) onSelect({ ...active.row, focus_seconds: point.seconds }); }} />
      <details><summary className="cursor-pointer text-sm">Observed values and comparison actors</summary><div className="max-h-64 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><thead><tr><th className="p-2">Source (s)</th><th className="p-2">Value ({active.unit})</th><th className="p-2">Comparison track</th><th className="p-2">Film</th></tr></thead><tbody>{active.points.flatMap((point, index) => point ? [<tr key={index}><td className="p-2">{point.seconds.toFixed(2)}</td><td className="p-2">{point.value.toFixed(2)}</td><td className="p-2 break-all">{point.opponent}</td><td className="p-2"><button className="text-primary underline" type="button" onClick={() => onSelect({ ...active.row, focus_seconds: point.seconds })}>Inspect pair at {point.seconds.toFixed(2)}s</button></td></tr>] : [])}</tbody></table></div></details>
    </section> : null}
    {te.length ? <section aria-label="Observed tight end usage" className="min-w-0 border-t border-border-soft pt-4"><h2 className="text-base font-semibold">TE block, chip and route observations</h2><p className="mt-1 text-xs text-muted-foreground">Explicit interaction states retain their event timestamps. Delay distributions appear with the other qualified measurements.</p><ol className="mt-3 flex flex-col gap-2">{te.map((state, index) => <li key={`${state.row.metric_id}-${index}`}><button className="text-sm text-primary underline" type="button" onClick={() => onSelect({ ...state.row, focus_seconds: state.seconds })}>Inspect {state.state} at {state.seconds.toFixed(2)}s</button><span className="ml-2 text-xs text-muted-foreground">Definition {state.definition}</span></li>)}</ol></section> : null}
    {landings.length ? <section aria-label="Qualified kick and punt landings" className="min-w-0 border-t border-border-soft pt-4"><h2 className="text-base font-semibold">Kick and punt landing locations</h2><p className="mt-1 text-xs text-muted-foreground">{landings.length} qualified ground landing observations in field yards. Airborne image positions are not projected onto the ground.</p><div className="relative mt-3"><FieldDiagram /><svg className="absolute inset-0 h-full w-full" viewBox="0 0 120 53.3" role="group" aria-label="Observed kick and punt landing field map">{landings.map((landing, index) => <circle key={landing.row.metric_id} cx={landing.x + 10} cy={(landing.y + 160 / 6) / (160 / 3) * 53.3} r=".8" fill="#e4bb50" role="button" tabIndex={0} aria-label={`Inspect landing ${index + 1}`} onClick={() => onSelect({ ...landing.row, focus_seconds: landing.seconds })} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect({ ...landing.row, focus_seconds: landing.seconds }); } }} />)}</svg></div><div className="mt-3 overflow-x-auto"><table className="w-full text-left text-xs tabular-nums"><thead><tr><th className="p-2">Field x/y (yd)</th><th className="p-2">Source (s)</th><th className="p-2">Definition</th><th className="p-2">Film</th></tr></thead><tbody>{landings.map((landing, index) => <tr key={landing.row.metric_id}><td className="p-2">{landing.x.toFixed(2)}, {landing.y.toFixed(2)}</td><td className="p-2">{landing.seconds.toFixed(2)}</td><td className="p-2">{landing.definition}</td><td className="p-2"><button className="text-primary underline" type="button" onClick={() => onSelect({ ...landing.row, focus_seconds: landing.seconds })}>Inspect landing {index + 1}</button></td></tr>)}</tbody></table></div></section> : null}
  </section>;
}
