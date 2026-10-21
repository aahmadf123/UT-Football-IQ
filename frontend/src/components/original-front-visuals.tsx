"use client";

import { useMemo, useState } from "react";
import type { AnalyticsGroup, FilmEvidence, SpatialProduct } from "@/lib/analytics-api";
import { canonicalShotId } from "@/lib/shot-id";
import { EChart } from "@/components/charts/echart";

const ROLES = ["LT", "LG", "C", "RG", "RT"] as const;
type Role = typeof ROLES[number];
type Point = { seconds: number; frame: number; x: number; y: number; forward: number; lateral: number };
type Actor = { role: Role; row: FilmEvidence; points: (Point | null)[] };
type Front = { key: string; snap: number; shot: string; los: number; direction: number; definition: string; actors: Partial<Record<Role, Actor>>; clocks: { seconds: number; frame: number }[] };
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const object = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;

/** Original named roles and exact source samples; no nearest-frame positions. */
export function originalFronts(groups: AnalyticsGroup[], products: SpatialProduct[] = []): Front[] {
  const fronts = new Map<string, Front>();
  const rejected = new Set<string>();
  const metrics = new Map<string, string>();
  for (const group of [...groups, ...products]) {
    if (group.metric_name !== "lineman_first_second_displacement" || group.unit !== "yd" || !group.definition_version) continue;
    for (const row of group.evidence) {
      const packet = row.metric_packet;
      if (!row.tracklet_id || !row.result_version || !finite(row.start_seconds) || !finite(row.end_seconds)
        || !packet || packet.actor_id !== row.tracklet_id || packet.track_id !== row.tracklet_id
        || !ROLES.includes(packet.original_role as Role) || ![-1, 1].includes(packet.attacking_direction as number)
        || !finite(packet.los_x) || packet.los_x < 0 || packet.los_x > 100 || !Array.isArray(packet.series)
        || !finite(packet.source_start_seconds) || !finite(packet.source_end_seconds)
        || packet.source_start_seconds < row.start_seconds || packet.source_end_seconds > row.end_seconds
        || packet.source_end_seconds < packet.source_start_seconds || !Array.isArray(packet.event_evidence)) continue;
      const snaps = packet.event_evidence.map(object).filter((event) => event?.event_type === "snap");
      const snap = snaps.length === 1 ? snaps[0] : null;
      const shot = canonicalShotId(snap?.shot_id);
      if (!snap || typeof snap.event_id !== "string" || !snap.event_id || shot === null
        || !["human", "manual", "real", "model"].includes(String(snap.source))
        || !finite(snap.timestamp_seconds) || snap.timestamp_seconds !== packet.source_start_seconds
        || packet.source_end_seconds > snap.timestamp_seconds + 1 + 1e-9) continue;
      const key = JSON.stringify([row.play_id, row.video_id, row.clip_id, row.result_version, group.definition_version, snap.event_id, snap.timestamp_seconds, shot]);
      const direction = packet.attacking_direction as number;
      const points: (Point | null)[] = [];
      const clocks = new Map<number, number>();
      const frames = new Set<number>();
      let origin: Point | null = null;
      let previous: Point | null = null;
      let sourcePrevious: Point | null = null;
      let conflicting = false;
      for (const raw of packet.series) {
        const point = object(raw);
        if (!point || point.visibility_state !== "observed" || canonicalShotId(point.shot_id) !== shot
          || !finite(point.timestamp_seconds) || point.timestamp_seconds < packet.source_start_seconds || point.timestamp_seconds > packet.source_end_seconds
          || !Number.isInteger(point.frame_number) || (point.frame_number as number) < 0
          || !finite(point.field_x) || point.field_x < -10 || point.field_x > 110
          || !finite(point.field_y) || Math.abs(point.field_y) > 80 / 3) {
          points.push(null); previous = null; continue;
        }
        const current: Point = { seconds: point.timestamp_seconds, frame: point.frame_number as number,
          x: point.field_x, y: point.field_y, forward: 0, lateral: 0 };
        if (clocks.has(current.seconds) || frames.has(current.frame)
          || (sourcePrevious && (current.seconds <= sourcePrevious.seconds || current.frame <= sourcePrevious.frame))) { conflicting = true; break; }
        clocks.set(current.seconds, current.frame); frames.add(current.frame);
        if (!origin) origin = current;
        current.forward = (current.x - origin.x) * direction;
        current.lateral = (current.y - origin.y) * direction;
        if (!finite(point.forward_yards) || !finite(point.lateral_yards)
          || Math.abs(point.forward_yards - current.forward) > .002 || Math.abs(point.lateral_yards - current.lateral) > .002) { conflicting = true; break; }
        if (previous && current.seconds - previous.seconds > .2 + 1e-9) points.push(null);
        points.push(current); previous = current; sourcePrevious = current;
      }
      if (conflicting || !origin || origin.seconds !== snap.timestamp_seconds || points.filter(Boolean).length < 2) { rejected.add(key); continue; }
      const role = packet.original_role as Role;
      const front = fronts.get(key) ?? { key, snap: snap.timestamp_seconds, shot, los: packet.los_x, direction, definition: group.definition_version, actors: {}, clocks: [] };
      const signature = JSON.stringify([row.tracklet_id, points]);
      const old = front.actors[role];
      const metricKey = JSON.stringify([key, role]);
      if (front.los !== packet.los_x || front.direction !== direction
        || (old && metrics.get(metricKey) !== signature)
        || Object.values(front.actors).some((actor) => actor.role !== role && actor.row.tracklet_id === row.tracklet_id)) rejected.add(key);
      if (!old) { front.actors[role] = { role, row, points }; metrics.set(metricKey, signature); }
      fronts.set(key, front);
    }
  }
  return [...fronts.values()].filter((front) => !rejected.has(front.key)).flatMap((front) => {
    const clocks = new Map<number, number>();
    const frameClocks = new Map<number, number>();
    for (const actor of Object.values(front.actors)) for (const point of actor.points) if (point) {
      if ((clocks.has(point.seconds) && clocks.get(point.seconds) !== point.frame)
        || (frameClocks.has(point.frame) && frameClocks.get(point.frame) !== point.seconds)) return [];
      clocks.set(point.seconds, point.frame);
      frameClocks.set(point.frame, point.seconds);
    }
    front.clocks = [...clocks].map(([seconds, frame]) => ({ seconds, frame })).sort((a, b) => a.seconds - b.seconds);
    return [front];
  });
}

export function OriginalFrontVisuals({ groups, products = [], position = "", onSelect }: {
  groups: AnalyticsGroup[]; products?: SpatialProduct[]; position?: string; onSelect: (row: FilmEvidence) => void;
}) {
  const fronts = useMemo(() => originalFronts(groups, products), [groups, products]);
  const [selected, setSelected] = useState("");
  const [clockIndex, setClockIndex] = useState(0);
  const active = fronts.find((front) => front.key === selected) ?? fronts[0];
  const clock = active?.clocks[Math.min(clockIndex, active.clocks.length - 1)];
  if ((position && position !== "OL") || !active || !clock) return null;
  const samples = ROLES.map((role) => ({ actor: active.actors[role], point: active.actors[role]?.points.find((point) => point?.seconds === clock.seconds && point.frame === clock.frame) ?? null }));
  const chartLimit = Math.max(1, ...Object.values(active.actors).flatMap((actor) => actor.points.flatMap((point) => point ? [Math.abs(point.forward), Math.abs(point.lateral)] : [])));
  const sampleActor = samples.find((sample) => sample.point)?.actor;
  const xOrigin = active.los - 8;
  const centerY = active.actors.C?.points.find((point) => point !== null)?.y ?? 0;
  return <section aria-label="Original offensive line comparisons" className="border-t border-border-soft pt-4">
    <h2 className="text-base font-semibold">Original five-lineman movement</h2>
    <p className="mt-1 text-xs text-muted-foreground">Roles remain LT, LG, C, RG and RT from the snap. Forward and lateral displacement are signed relative to the attacking direction. Each point is an observed source sample; missing actors stay unobserved. Geometry does not grade protection or assignments. Definition {active.definition}.</p>
    <label className="mt-3 flex flex-col gap-1 text-xs">Measured front<select className="rounded border border-border-soft bg-background p-2" value={active.key} onChange={(event) => { setSelected(event.target.value); setClockIndex(0); }}>{fronts.map((front, index) => <option key={front.key} value={front.key}>Play {index + 1} · {Object.keys(front.actors).length}/5 named trajectories · snap {front.snap.toFixed(2)}s</option>)}</select></label>
    <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-5">{ROLES.map((role) => {
      const actor = active.actors[role];
      return <section key={role} aria-label={`${role} observed displacement`} className="min-w-0 rounded border border-border-soft p-2"><h3 className="text-sm font-semibold">{role}</h3>{actor ? <><EChart label={`${role} observed forward and lateral displacement in yards after snap`} option={{
        animation: false, grid: { left: 35, right: 8, top: 15, bottom: 42 },
        xAxis: { type: "value", name: "After snap (s)", min: 0, max: 1, nameLocation: "middle", nameGap: 26 },
        yAxis: { type: "value", min: -chartLimit, max: chartLimit, name: "yd" },
        tooltip: { trigger: "item" }, series: ["forward", "lateral"].map((name) => ({ name, type: "line", connectNulls: false, showSymbol: true, symbolSize: 4,
          data: actor.points.map((point) => point ? [point.seconds - active.snap, name === "forward" ? point.forward : point.lateral] : null) })),
      }} /><p className="text-xs text-muted-foreground">Gold: forward · blue: lateral</p></> : <p className="py-8 text-xs text-muted-foreground">Unobserved</p>}</section>;
    })}</div>
    <section aria-label="Synchronized original line geometry" className="mt-4">
      <h3 className="text-sm font-semibold">Measured trench geometry</h3>
      <label className="mt-2 flex flex-col gap-1 text-xs">Source frame {clock.frame} · {clock.seconds.toFixed(3)}s<input aria-label="Trench source sample" type="range" min={0} max={active.clocks.length - 1} step={1} value={Math.min(clockIndex, active.clocks.length - 1)} onChange={(event) => setClockIndex(Number(event.target.value))} /></label>
      <svg role="img" aria-label="Original line at the selected exact source frame" viewBox="0 0 16 20" className="mt-2 h-64 w-full rounded bg-primary/5">
        <line x1={8} x2={8} y1={0} y2={20} stroke="#94a3b8" strokeDasharray=".2 .2" strokeWidth=".04" />
        <text x={8.2} y={1} fontSize=".6" fill="#cbd5e1">LOS · attack {active.direction === 1 ? "→" : "←"}</text>
        {samples.map(({ actor, point }, index) => point ? <g key={ROLES[index]}><circle cx={point.x - xOrigin} cy={10 - (point.y - centerY) * active.direction} r=".25" fill="#e4bb50" /><text x={point.x - xOrigin + .4} y={10 - (point.y - centerY) * active.direction + .15} fill="#cbd5e1" fontSize=".6">{actor?.role}</text></g> : null)}
      </svg>
      <p className="mt-1 text-xs text-muted-foreground">Window: LOS ±8 yd longitudinally, lateral center {centerY.toFixed(2)} yd ±10 yd. Positions outside the window remain in the table. Source shot {active.shot}. No position is carried forward between samples.</p>
      {sampleActor ? <button type="button" className="mt-2 text-sm text-primary underline" onClick={() => onSelect({ ...sampleActor.row, focus_seconds: clock.seconds })}>Inspect film at {clock.seconds.toFixed(3)}s</button> : null}
      <table className="mt-3 w-full text-left text-xs tabular-nums"><caption className="sr-only">Exact source-frame line positions</caption><thead><tr><th className="p-2">Original role</th><th className="p-2">Field x/y (yd)</th><th className="p-2">Forward/lateral (yd)</th></tr></thead><tbody>{samples.map(({ point }, index) => <tr key={ROLES[index]}><th className="p-2">{ROLES[index]}</th><td className="p-2">{point ? `${point.x.toFixed(2)}, ${point.y.toFixed(2)}` : "Unobserved at this source frame"}</td><td className="p-2">{point ? `${point.forward.toFixed(2)}, ${point.lateral.toFixed(2)}` : "Unknown"}</td></tr>)}</tbody></table>
    </section>
  </section>;
}
