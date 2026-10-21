"use client";

import { useMemo, useState } from "react";
import type { AnalyticsGroup, FilmEvidence, SpatialProduct } from "@/lib/analytics-api";
import { FieldDiagram } from "@/components/field-diagram";
import { canonicalShotId } from "@/lib/shot-id";

const obj = (v: unknown): Record<string, unknown> | null => v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : null;
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const visible = (v: unknown) => v === "observed" || v === "visible";
const inside = (x: unknown, y: unknown): boolean => finite(x) && x >= -10 && x <= 110 && finite(y) && Math.abs(y) <= 160 / 6;
const sy = (y: number) => (y + 160 / 6) / (160 / 3) * 53.3;
const names = new Set(["observed_run_path", "signed_forward_gain", "pre_contact_yards", "post_contact_yards", "handoff_to_los", "actual_entry_gap"]);
type Point = { x: number; y: number; seconds: number; shot: string; frame: number | null; interpolated: boolean };
type Front = { seconds: number; los: number; direction: number; roles: { role: string; id: string; x: number; y: number }[]; gaps: { label: string; lo: number; hi: number }[] };
export type ObservedRun = { key: string; row: FilmEvidence; definition: string; points: (Point | null)[]; partial: boolean; contact: Point | null; front: Front | null; entry: (Point & { gap: string | null }) | null };
function eventAt(events: unknown, id: unknown, kinds: string[], seconds: number, shot: string) {
  return typeof id === "string" && !!id && Array.isArray(events) && events.some((raw) => {
    const e = obj(raw);
    return e && e.event_id === id && kinds.includes(String(e.event_type)) && e.timestamp_seconds === seconds
      && canonicalShotId(e.shot_id) === shot && ["human", "manual", "real", "model"].includes(String(e.source));
  });
}
function at(points: (Point | null)[], seconds: number): Point | null {
  for (let i = 0; i < points.length; i++) {
    const p = points[i]; if (!p) continue;
    if (p.seconds === seconds) return p;
    const q = points[i + 1];
    if (q && p.seconds < seconds && seconds < q.seconds && q.seconds - p.seconds <= .2 + 1e-9 && p.shot === q.shot) {
      const r = (seconds - p.seconds) / (q.seconds - p.seconds);
      return { seconds, shot: p.shot, frame: null, interpolated: true, x: p.x + r * (q.x - p.x), y: p.y + r * (q.y - p.y) };
    }
  }
  return null;
}
function originalFront(raw: unknown, events: unknown, shot: string, row: FilmEvidence): Front | null {
  const f = obj(raw); const roles = obj(f?.roles);
  if (!f || !roles || f.definition_basis !== "original_named_snap_centers" || canonicalShotId(f.shot_id) !== shot
    || !finite(f.timestamp_seconds) || f.timestamp_seconds < row.start_seconds || f.timestamp_seconds > row.end_seconds
    || !finite(f.los_x) || f.los_x < 0 || f.los_x > 100 || ![1, -1].includes(f.attacking_direction as number)
    || !eventAt(events, f.snap_event_id, ["snap"], f.timestamp_seconds, shot)) return null;
  const entries = Object.entries(roles).flatMap(([role, raw]) => {
    const p = obj(raw); return p && inside(p.field_x, p.field_y) && typeof p.track_id === "string" && p.track_id
      ? [{ role, id: p.track_id, x: p.field_x as number, y: p.field_y as number }] : [];
  });
  const order = ["LT", "LG", "C", "RG", "RT"].map((role) => entries.find((p) => p.role === role));
  if (entries.length !== Object.keys(roles).length || entries.some((p) => !["LT", "LG", "C", "RG", "RT", "LTE", "RTE"].includes(p.role)) || order.some((p) => !p) || new Set(entries.map((p) => p.id)).size !== entries.length
    || order.slice(1).some((p, i) => (order[i]!.y - p!.y) * (f.attacking_direction as number) <= 0)) return null;
  const pairs: Record<string, string[]> = { left_A: ["LG", "C"], left_B: ["LT", "LG"], right_A: ["C", "RG"], right_B: ["RG", "RT"], left_C: ["LTE", "LT"], right_C: ["RT", "RTE"] };
  const gaps = Array.isArray(f.gaps) ? f.gaps.flatMap((raw) => {
    const g = obj(raw); const pair = g && pairs[String(g.gap_label)]?.map((r) => entries.find((p) => p.role === r));
    if (!g || !pair || pair.some((p) => !p) || !Array.isArray(g.initial_pair) || g.initial_pair.length !== 2
      || !pair.every((p) => g.initial_pair instanceof Array && g.initial_pair.includes(p!.id))
      || g.y_min !== Math.min(...pair.map((p) => p!.y)) || g.y_max !== Math.max(...pair.map((p) => p!.y))) return [];
    return [{ label: String(g.gap_label), lo: g.y_min as number, hi: g.y_max as number }];
  }) : [];
  return { seconds: f.timestamp_seconds, los: f.los_x, direction: f.attacking_direction as number, roles: entries, gaps };
}

/** Qualified API evidence only. Never manufacture a path, contact, or gap. */
export function observedRunEvidence(groups: AnalyticsGroup[], products: SpatialProduct[] = []): ObservedRun[] {
  const selected = new Map<string, ObservedRun>();
  for (const group of [...groups, ...products]) {
    if (!names.has(group.metric_name) || !group.definition_version) continue;
    for (const row of group.evidence) {
      const packet = row.metric_packet; const path = obj(packet?.run_path);
      if (!packet || !path || path.clock_basis !== "verified_native_pts" || path.series_interpolation !== "none" || !row.tracklet_id || !row.result_version || !row.play_id || path.actor_id !== row.tracklet_id
        || packet.track_id !== row.tracklet_id || !Array.isArray(path.trajectory) || !finite(path.source_start_seconds)
        || !finite(path.source_end_seconds) || path.source_end_seconds < path.source_start_seconds
        || path.source_start_seconds < row.start_seconds || path.source_end_seconds > row.end_seconds) continue;
      const points: (Point | null)[] = []; let previous: Point | null = null; let native: Point | null = null; let broken = false;
      for (const raw of path.trajectory) {
        const p = obj(raw); const shot = canonicalShotId(p?.shot_id);
        if (!p || shot === null || !visible(p.visibility_state) || p.interpolated === true || p.predicted === true
          || !finite(p.frame_number) || !Number.isInteger(p.frame_number) || p.frame_number < 0
          || !inside(p.field_x, p.field_y) || !finite(p.timestamp_seconds)
          || p.timestamp_seconds < path.source_start_seconds || p.timestamp_seconds > path.source_end_seconds) {
          points.push(null); previous = null; broken = true; continue;
        }
        const point = { x: p.field_x as number, y: p.field_y as number, seconds: p.timestamp_seconds, shot, frame: p.frame_number, interpolated: false };
        if (native && (point.seconds <= native.seconds || point.frame <= native.frame!)) { points.push(null); previous = null; broken = true; continue; }
        native = point;
        if (previous && (point.seconds <= previous.seconds || point.seconds - previous.seconds > .2 + 1e-9 || point.shot !== previous.shot)) { points.push(null); broken = true; }
        points.push(point); previous = point;
      }
      const first = points.find((p): p is Point => p !== null);
      if (!first || first.seconds - path.source_start_seconds > .2 + 1e-9 || !eventAt(path.event_evidence, path.possession_event_id, ["handoff", "possession_start"], path.source_start_seconds, first.shot)) continue;
      const front = originalFront(packet.original_front, path.event_evidence, first.shot, row);
      const contact = obj(packet.first_contact); const location = contact && finite(contact.timestamp_seconds) ? at(points, contact.timestamp_seconds) : null;
      const marked = contact && location && contact.actor_id === row.tracklet_id && contact.explicit_first_contact === true && visible(contact.visibility_state)
        && eventAt([contact], contact.event_id, ["first_contact", "contact"], location.seconds, location.shot) && inside(contact.field_x, contact.field_y)
        && Math.abs(location.x - (contact.field_x as number)) < 1e-5 && Math.abs(location.y - (contact.field_y as number)) < 1e-5 ? location : null;
      const rawEntry = obj(packet.entry); const entrySeconds = rawEntry?.timestamp_seconds ?? packet.entry_seconds;
      const entry = finite(entrySeconds) ? at(points, entrySeconds) : null;
      const label = rawEntry?.gap_label ?? packet.gap_label;
      const edge = front && typeof label === "string" && ["left_edge", "right_edge"].includes(label)
        ? front.roles.filter((p) => (label === "left_edge" ? ["LTE", "LT"] : ["RTE", "RT"]).includes(p.role))
          .sort((a, b) => (b.y - a.y) * front.direction * (label === "left_edge" ? 1 : -1))[0] : null;
      const gap = front && entry && typeof label === "string" && (front.gaps.some((g) => g.label === label && g.lo < entry.y && entry.y < g.hi)
        || (edge && (entry.y - edge.y) * front.direction * (label === "left_edge" ? 1 : -1) > 0)) ? label : null;
      const entryY = rawEntry?.field_y ?? packet.entry_field_y;
      const validEntry = entry && front && finite(entryY) && Math.abs(entry.y - entryY) < 1e-5 && Math.abs(entry.x - front.los) < 1e-5 ? { ...entry, gap } : null;
      const key = JSON.stringify([row.play_id, row.clip_id, row.result_version, row.tracklet_id, path.possession_event_id, group.definition_version]);
      const last = points.at(-1);
      const terminal = Array.isArray(path.event_evidence) && path.event_evidence.some((raw) => {
        const e = obj(raw);
        return e && last && last.seconds === path.source_end_seconds
          && eventAt(path.event_evidence, e.event_id, ["end_of_play", "tackle", "out_of_bounds", "touchdown"], path.source_end_seconds as number, last.shot);
      });
      const run = { key, row, definition: group.definition_version, points, front, contact: marked, entry: validEntry,
        partial: broken || !terminal || path.partial_observation === true || path.measurement_complete !== true };
      const old = selected.get(key);
      if (!old || (!old.entry && run.entry) || (!old.contact && run.contact)) selected.set(key, run);
    }
  }
  return [...selected.values()];
}

/** Count distinct canonical plays per observed 2-yard cell, avoiding FPS weighting. */
export function runDensity(runs: ObservedRun[]) {
  if (new Set(runs.map((run) => run.definition)).size > 1) return [];
  const bins = new Map<string, { x: number; y: number; plays: Set<string>; samples: { run: ObservedRun; point: Point }[] }>();
  for (const run of runs) for (const point of run.points) if (point && !point.interpolated) {
    const x = Math.min(108, Math.floor((point.x + 10) / 2) * 2 - 10), y = Math.floor((point.y + 160 / 6) / 2) * 2 - 160 / 6;
    const key = `${x}:${y}`; const cell = bins.get(key) ?? { x, y, plays: new Set<string>(), samples: [] };
    cell.plays.add(run.row.play_id); cell.samples.push({ run, point }); bins.set(key, cell);
  }
  return [...bins.values()];
}

export function RunEvidenceVisuals({ groups, products = [], position = "", onSelect }: { groups: AnalyticsGroup[]; products?: SpatialProduct[]; position?: string; onSelect: (row: FilmEvidence) => void }) {
  const runs = useMemo(() => !position || position === "RB" ? observedRunEvidence(groups, products) : [], [groups, products, position]);
  const [selected, setSelected] = useState("");
  const active = runs.find((r) => r.key === selected) ?? runs[0];
  const bins = useMemo(() => runDensity(runs.filter((run) => run.definition === active?.definition)), [runs, active?.definition]);
  if (position && position !== "RB") return null;
  if (!active) return <section aria-label="Observed running back evidence"><h2 className="font-semibold">Observed run paths and contact</h2><p className="text-sm text-muted-foreground">Qualified carrier paths are unavailable for this selection. Contact and original gaps require their own source evidence.</p></section>;
  const segments = active.points.reduce<Point[][]>((all, p) => { if (!p) all.push([]); else all[all.length - 1].push(p); return all; }, [[]]);
  const inspect = (run: ObservedRun, seconds: number) => onSelect({ ...run.row, focus_seconds: seconds });
  const gaps = new Map<string, Set<string>>(); for (const run of runs) if (run.definition === active.definition && run.entry?.gap) { const plays = gaps.get(run.entry.gap) ?? new Set<string>(); plays.add(run.row.play_id); gaps.set(run.entry.gap, plays); }
  return <section aria-label="Observed running back evidence" className="border-t border-border-soft pt-4">
    <h2 className="text-base font-semibold">Observed run paths and contact</h2>
    <p className="mt-1 text-xs text-muted-foreground">Two-yard density cells count distinct observed plays visiting measured field locations. Sampling coverage varies; these are not opportunity rates or open-lane measurements. Contact is marked only by explicit first-contact evidence. Density uses native samples; contact locations may interpolate between same-shot observations no more than 200 ms apart.</p>
    <label className="mt-3 flex flex-col gap-1 text-xs">Observed carry<select className="rounded border border-border-soft bg-background p-2" value={active.key} onChange={(e) => setSelected(e.target.value)}>{runs.map((run, i) => <option key={run.key} value={run.key}>Carry {i + 1}{run.partial ? " · observed prefix" : " · observed through terminal"}</option>)}</select></label>
    <p className="mt-2 text-xs text-muted-foreground">Definition {active.definition}. {active.partial ? "Partial observed prefix; no complete run outcome is inferred." : "Observed through a qualified terminal event."} Missing observations, shots and gaps above 200 ms break paths.</p>
    <div className="relative mt-3"><FieldDiagram /><svg className="absolute inset-0 h-full w-full" viewBox="0 0 120 53.3" role="img" aria-label="Observed run density, selected path and explicit contact map">
      {bins.map((cell) => <rect key={`${cell.x}:${cell.y}`} x={cell.x + 10} y={sy(cell.y)} width={Math.min(2, 110 - cell.x)} height={Math.min(2, 160 / 6 - cell.y) / (160 / 3) * 53.3} fill="#e4bb50" opacity={Math.min(.65, .12 + .1 * cell.plays.size)} />)}
      {active.front ? <><line x1={active.front.los + 10} x2={active.front.los + 10} y1={0} y2={53.3} stroke="#e4bb50" strokeWidth=".2" />{active.front.gaps.map((g) => <line key={g.label} x1={active.front!.los + 10} x2={active.front!.los + 10} y1={sy(g.lo)} y2={sy(g.hi)} stroke="#e4bb50" strokeWidth=".7"><title>{g.label.replaceAll("_", " ")} · original named centers</title></line>)}{active.front.roles.map((p) => <circle key={p.role} cx={p.x + 10} cy={sy(p.y)} r=".45" fill="#cbd5e1"><title>Original {p.role}</title></circle>)}</> : null}
      {segments.map((points, i) => points.length > 1 ? <polyline key={i} points={points.map((p) => `${p.x + 10},${sy(p.y)}`).join(" ")} fill="none" stroke="#38bdf8" strokeWidth=".4" /> : null)}
      {active.entry ? <circle cx={active.entry.x + 10} cy={sy(active.entry.y)} r=".65" fill="#e4bb50" /> : null}
      {active.contact ? <circle cx={active.contact.x + 10} cy={sy(active.contact.y)} r=".85" fill="#fb7185"><title>Explicit first contact</title></circle> : null}
    </svg></div>
    <p className="mt-2 text-xs">Blue: selected path · pink: explicit first contact · gold: LOS/original gaps and density · gray: original named snap centers.</p>
    <div className="mt-3 flex flex-wrap gap-3 text-sm"><button type="button" className="text-primary underline" onClick={() => inspect(active, active.points.find((p) => p)!.seconds)}>Inspect carry start</button>{active.contact ? <button type="button" className="text-primary underline" onClick={() => inspect(active, active.contact!.seconds)}>Inspect first contact at {active.contact.seconds.toFixed(2)}s</button> : <span>First contact unavailable</span>}{active.front ? <button type="button" className="text-primary underline" onClick={() => inspect(active, active.front!.seconds)}>Inspect original snap geometry</button> : <span>Original gap geometry unavailable</span>}{active.entry ? <button type="button" className="text-primary underline" onClick={() => inspect(active, active.entry!.seconds)}>Inspect actual LOS entry{active.entry.gap ? ` · ${active.entry.gap.replaceAll("_", " ")}` : " · gap unknown"}</button> : <span>Actual LOS entry unavailable</span>}</div>
    {active.contact?.interpolated ? <p className="mt-2 text-xs">Contact location interpolated between supported native observations; event time is explicit.</p> : null}<p className="mt-2 text-xs text-muted-foreground">Original gaps are between named snap centers, not measured clear openings or intended assignments.</p>
    {gaps.size ? <div className="mt-3 flex flex-wrap gap-4 text-sm" aria-label="Actual original-gap entries">{[...gaps].map(([gap, plays]) => <span key={gap}>{gap.replaceAll("_", " ")}: {plays.size} observed {plays.size === 1 ? "play" : "plays"}</span>)}</div> : null}
    <details className="mt-3"><summary className="cursor-pointer text-sm">Measured path and density film evidence</summary><table className="mt-2 w-full text-left text-xs"><thead><tr><th>Native frame</th><th>Source (s)</th><th>Field x/y (yd)</th><th>Film</th></tr></thead><tbody>{active.points.flatMap((point, i) => point ? [<tr key={i}><td>{point.frame}</td><td>{point.seconds.toFixed(3)}</td><td>{point.x.toFixed(2)}, {point.y.toFixed(2)}</td><td><button type="button" className="text-primary underline" onClick={() => inspect(active, point.seconds)}>Inspect path at {point.seconds.toFixed(3)}s</button></td></tr>] : [])}</tbody></table><div className="mt-3 flex flex-wrap gap-2">{bins.map((cell) => <button key={`${cell.x}:${cell.y}`} type="button" className="text-xs text-primary underline" onClick={() => { setSelected(cell.samples[0].run.key); inspect(cell.samples[0].run, cell.samples[0].point.seconds); }}>Inspect density cell {cell.x.toFixed(0)}/{cell.y.toFixed(0)}yd · {cell.plays.size} {cell.plays.size === 1 ? "play" : "plays"}</button>)}</div></details>
  </section>;
}
