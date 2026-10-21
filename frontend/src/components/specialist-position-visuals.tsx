"use client";

import { useMemo } from "react";
import type { AnalyticsGroup, MetricEvidence } from "@/lib/analytics-api";
import { EChart } from "@/components/charts/echart";
import { FieldDiagram } from "@/components/field-diagram";

type Event = { id: string; type: string; seconds: number; source: string; shot?: string | number; row: MetricEvidence };
type Timeline = { row: MetricEvidence; events: Event[]; definition: string; unknown?: number; eligible: number };
type Release = { row: MetricEvidence; x: number; y: number; seconds: number; definition: string };
type Gap = { row: MetricEvidence; label: string; seconds: number; width: number; definition: string };
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const visible = (value: unknown) => !["lost", "occluded", "offscreen", "unobserved"].includes(String(value ?? ""));
const QB_METRICS = new Set(["time_to_throw", "time_to_first_pressure", "ball_holding_duration", "tackle_box_residence"]);

function actualEvents(row: MetricEvidence): Event[] {
  const raw = row.metric_packet?.event_evidence;
  if (!row.result_version || !Array.isArray(raw)) return [];
  return raw.flatMap((value) => {
    const event = object(value);
    if (!event || typeof event.event_id !== "string" || !event.event_id || typeof event.event_type !== "string"
      || typeof event.source !== "string" || !event.source || !finite(event.timestamp_seconds)
      || event.timestamp_seconds < row.start_seconds || event.timestamp_seconds > row.end_seconds) return [];
    const shot = typeof event.shot_id === "string" || typeof event.shot_id === "number" ? event.shot_id : undefined;
    return [{ id: event.event_id, type: event.event_type, seconds: event.timestamp_seconds, source: event.source, shot, row }];
  }).sort((a, b) => a.seconds - b.seconds);
}

/** Every marker comes from event evidence attached to a qualified measurement. */
export function eventTimelines(groups: AnalyticsGroup[]): Timeline[] {
  const timelines = new Map<string, Timeline>();
  for (const group of groups) {
    if (!QB_METRICS.has(group.metric_name) || group.unit !== "s" || !group.definition_version) continue;
    for (const row of group.evidence) {
      const events = actualEvents(row);
      if (!events.length || !row.tracklet_id) continue;
      const key = JSON.stringify([row.play_id, row.clip_id, row.tracklet_id, row.result_version, group.definition_version]);
      const previous = timelines.get(key);
      if (previous) {
        const ids = new Set(previous.events.map((event) => event.id));
        previous.events = [...previous.events, ...events.filter((event) => !ids.has(event.id))].sort((a, b) => a.seconds - b.seconds);
      } else timelines.set(key, { row, events, definition: group.definition_version,
        unknown: group.unknown_count, eligible: group.eligible_count ?? group.n });
    }
  }
  return [...timelines.values()];
}

/** Never extrapolate a release location across missing observations or a cut. */
export function releaseLocations(groups: AnalyticsGroup[]): Release[] {
  return groups.filter((group) => group.metric_name === "time_to_throw" && group.unit === "s" && group.definition_version)
    .flatMap((group) => group.evidence.flatMap((row) => {
      const release = actualEvents(row).find((event) => ["throw", "release", "pass_release", "forward_pass_release"].includes(event.type));
      const trajectory = row.metric_packet?.trajectory;
      if (!release || row.right_censored || !row.tracklet_id || !Array.isArray(trajectory)) return [];
      const near = trajectory.map(object).filter((p): p is Record<string, unknown> => Boolean(p)
        && finite(p!.timestamp_seconds) && Math.abs(p!.timestamp_seconds as number - release.seconds) <= .1 + 1e-9);
      const shots = new Set(near.map((p) => p.shot_id));
      if (shots.size > 1) return [];
      const point = near.filter((p) => ["visible", "observed"].includes(String(p.visibility_state))
        && p.shot_id != null && (release.shot == null || p.shot_id === release.shot)
        && finite(p.field_x) && p.field_x >= -10 && p.field_x <= 110
        && finite(p.field_y) && Math.abs(p.field_y) <= 160 / 6)
        .sort((a, b) => Math.abs(a.timestamp_seconds as number - release.seconds) - Math.abs(b.timestamp_seconds as number - release.seconds))[0];
      if (point && near.some((p) => (p.timestamp_seconds as number) >= Math.min(point.timestamp_seconds as number, release.seconds)
        && (p.timestamp_seconds as number) <= Math.max(point.timestamp_seconds as number, release.seconds)
        && (!visible(p.visibility_state) || p.shot_id !== point.shot_id || !finite(p.field_x) || !finite(p.field_y)))) return [];
      return point ? [{ row, x: point.field_x as number, y: point.field_y as number,
        seconds: release.seconds, definition: group.definition_version! }] : [];
    }));
}

/** Widths refer to the producer's explicit LOS section and initial gap subject. */
export function gapObservations(groups: AnalyticsGroup[]): Gap[] {
  return groups.filter((group) => group.metric_name === "clear_lane_width" && group.unit === "yd" && group.definition_version)
    .flatMap((group) => group.evidence.flatMap((row) => {
      const packet = row.metric_packet;
      const region = object(packet?.measurement_region);
      if (!row.result_version || row.right_censored || typeof packet?.gap_label !== "string"
        || typeof packet.subject_id !== "string" || region?.cross_section !== "LOS"
        || !finite(region.los_x) || !finite(region.y_min) || !finite(region.y_max)
        || region.y_max <= region.y_min || !Array.isArray(packet.series)) return [];
      const sectionWidth = region.y_max - region.y_min;
      return packet.series.flatMap((raw) => {
        const p = object(raw);
        if (!p || !finite(p.timestamp_seconds) || p.timestamp_seconds < row.start_seconds || p.timestamp_seconds > row.end_seconds
          || !finite(p.width_yards) || p.width_yards < 0 || p.width_yards > sectionWidth + 1e-9 || !visible(p.visibility_state)) return [];
        return [{ row, seconds: p.timestamp_seconds, width: p.width_yards,
          label: `${packet.gap_label} · ${row.play_id.slice(0, 8)} · ${packet.subject_id}`, definition: group.definition_version! }];
      });
    }));
}

export function SpecialistPositionVisuals({ groups, position = "", onSelect }: {
  groups: AnalyticsGroup[]; position?: string; onSelect: (row: MetricEvidence) => void;
}) {
  const timelines = useMemo(() => !position || position === "QB" ? eventTimelines(groups) : [], [groups, position]);
  const releases = useMemo(() => !position || position === "QB" ? releaseLocations(groups) : [], [groups, position]);
  const gaps = useMemo(() => !position || position === "OL" ? gapObservations(groups) : [], [groups, position]);
  const labels = useMemo(() => [...new Set(gaps.map((gap) => gap.label))], [gaps]);
  const timestamps = useMemo(() => [...new Set(gaps.map((gap) => gap.seconds))].sort((a, b) => a - b), [gaps]);
  const heatmap = useMemo(() => ({ animation: false, grid: { left: 150, right: 30, top: 24, bottom: 55 },
    xAxis: { type: "category", name: "Source time (s)", data: timestamps.map(String), boundaryGap: true },
    yAxis: { type: "category", data: labels, boundaryGap: true },
    visualMap: { min: 0, max: Math.max(1, ...gaps.map((gap) => gap.width)), orient: "horizontal", bottom: 0, inRange: { color: ["#29394b", "#e4bb50"] } },
    tooltip: { trigger: "item" }, series: [{ type: "heatmap", data: gaps.map((gap) => [timestamps.indexOf(gap.seconds), labels.indexOf(gap.label), gap.width]) }] }), [gaps, labels, timestamps]);
  if (!timelines.length && !releases.length && !gaps.length) return null;
  const inspect = (row: MetricEvidence, seconds: number) => onSelect({ ...row, focus_seconds: seconds });
  return <section aria-label="Qualified specialist evidence" className="grid gap-6 xl:grid-cols-2">
    {timelines.map((timeline) => <section className="min-w-0 border-t border-border-soft pt-4" key={`${timeline.row.metric_id}-${timeline.definition}`} aria-label="Quarterback event timeline">
      <h2 className="text-base font-semibold">Snap, pressure, release and exit evidence</h2>
      <p className="mt-1 text-xs text-muted-foreground">1 contributing play · group population: {timeline.eligible} eligible measurements · {timeline.unknown == null ? "unknown coverage unavailable" : `${timeline.unknown} unknown`} · source seconds · Definition {timeline.definition}. Only observed events appear; absence does not mean pressure-free.</p>
      <ol className="mt-3 flex flex-wrap gap-2">{timeline.events.map((event) => <li key={event.id}><button type="button" className="rounded border border-border-soft px-3 py-2 text-left text-sm hover:border-primary focus-visible:outline-primary" aria-label={`Inspect ${event.type} at ${event.seconds.toFixed(2)} seconds`} onClick={() => inspect(event.row, event.seconds)}><span className="block capitalize">{event.type.replaceAll("_", " ")}</span><span className="tabular-nums text-muted-foreground">{event.seconds.toFixed(2)} s</span></button></li>)}</ol>
      <table className="mt-3 w-full text-left text-xs"><caption className="sr-only">Observed source events and provenance</caption><thead><tr><th className="p-2">Event</th><th className="p-2">Source time (s)</th><th className="p-2">Evidence source</th></tr></thead><tbody>{timeline.events.map((event) => <tr key={event.id}><td className="p-2">{event.type}</td><td className="p-2 tabular-nums">{event.seconds.toFixed(2)}</td><td className="p-2">{event.source}</td></tr>)}</tbody></table>
    </section>)}
    {releases.length ? <section className="min-w-0 border-t border-border-soft pt-4" aria-label="Quarterback release locations"><h2 className="text-base font-semibold">Observed release locations</h2><p className="mt-1 text-xs text-muted-foreground">n={releases.length} qualified release observations · canonical field yards · Definition {[...new Set(releases.map((r) => r.definition))].join(", ")}. Points require a visible observation within 100 ms of release in the same view.</p><div className="relative mt-3"><FieldDiagram /><svg viewBox="0 0 120 53.3" className="absolute inset-0 h-full w-full" aria-label="Release locations on the field" role="group">{releases.map((release) => <circle key={release.row.metric_id} cx={release.x + 10} cy={(release.y + 160 / 6) / (160 / 3) * 53.3} r=".65" fill="#e4bb50" role="button" tabIndex={0} aria-label={`Inspect release at ${release.seconds.toFixed(2)} seconds`} onClick={() => inspect(release.row, release.seconds)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inspect(release.row, release.seconds); } }} />)}</svg></div><table className="mt-3 w-full text-left text-xs"><caption className="sr-only">Release coordinates and film</caption><thead><tr><th className="p-2">Source (s)</th><th className="p-2">Field x/y (yd)</th><th className="p-2">Film</th></tr></thead><tbody>{releases.map((r) => <tr key={r.row.metric_id}><td className="p-2">{r.seconds.toFixed(2)}</td><td className="p-2">{r.x.toFixed(2)}, {r.y.toFixed(2)}</td><td className="p-2"><button type="button" className="text-primary underline" onClick={() => inspect(r.row, r.seconds)}>Inspect release</button></td></tr>)}</tbody></table></section> : null}
    {gaps.length ? <section className="min-w-0 border-t border-border-soft pt-4 xl:col-span-2" aria-label="Line gap width heatmap"><h2 className="text-base font-semibold">Clear lane width at the line of scrimmage</h2><p className="mt-1 text-xs text-muted-foreground">{new Set(gaps.map((g) => g.row.play_id)).size} contributing plays · {gaps.length} measured sections · yards · Definition {[...new Set(gaps.map((g) => g.definition))].join(", ")}. A clear section does not establish a successful running path. Initial formation gap names remain fixed.</p><EChart option={heatmap} label="Gap width heatmap; exact source seconds and clear width in yards. Accessible observations follow." onSelect={(index) => { const gap = gaps[index]; if (gap) inspect(gap.row, gap.seconds); }} /><details><summary className="cursor-pointer text-sm">Measured sections and film evidence</summary><div className="max-h-72 overflow-auto"><table className="w-full text-left text-xs tabular-nums"><thead><tr><th className="p-2">Initial gap / play / subject</th><th className="p-2">Source (s)</th><th className="p-2">Clear width (yd)</th><th className="p-2">Film</th></tr></thead><tbody>{gaps.map((gap, i) => <tr key={`${gap.row.metric_id}-${i}`}><td className="p-2">{gap.label}</td><td className="p-2">{gap.seconds.toFixed(2)}</td><td className="p-2">{gap.width.toFixed(2)}</td><td className="p-2"><button type="button" className="text-primary underline" onClick={() => inspect(gap.row, gap.seconds)}>Inspect section</button></td></tr>)}</tbody></table></div></details></section> : null}
  </section>;
}
