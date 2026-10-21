"use client";

import type { OverlayTracklet } from "@/lib/types";
import { canonicalShotId } from "@/lib/shot-id";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

export const PREDICTION_CLOCKS = ["snap", "handoff", "pass_release", "catch"];
const OL = ["LT", "LG", "C", "RG", "RT"];
const DL = ["DL", "DE", "DT", "NT"];
const LB = ["LB", "ILB", "OLB", "MLB"];
const DEFENSE = [...DL, ...LB, "CB", "DB", "S", "FS", "SS", "NB"];
export type PredictionContextDraft = {
  enabled: boolean; available: string; playDesign: string; qb: string; target: string; possessor: string;
  defenders: string[]; defendersComplete: boolean; forward: boolean; completed: boolean;
  defensiveRoles: Record<string, string>; gaps: Record<string, string>; frontComplete: boolean;
  callsKnown: boolean; callsAvailable: string; rushers: string[]; blitzers: string[]; blockers: string[];
  down: string; distance: string; ballY: string;
};
export const emptyPredictionContext = (): PredictionContextDraft => ({ enabled: false, available: "", playDesign: "", qb: "", target: "", possessor: "", defenders: [], defendersComplete: false,
  forward: false, completed: false, defensiveRoles: {}, gaps: {}, frontComplete: false,
  callsKnown: false, callsAvailable: "", rushers: [], blitzers: [], blockers: [], down: "", distance: "", ballY: "" });
type ContextInput = { type: string; value: PredictionContextDraft; tracks: OverlayTracklet[]; actor: string; localTime: number;
  clipStart?: number; shot: string; originalRoles?: Record<string, string>; los?: string; direction?: string };
const numeric = (value: string) => value.trim() !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
function sideAllows(track: OverlayTracklet | undefined, side: string) {
  return Boolean(track) && [track?.side_of_ball, track?.team_label].every((value) => !["offense", "defense", "special_teams"].includes(value ?? "") || value === side);
}

/** Explicit source context only; never creates a feature value or outcome label. */
export function buildPredictionContext({ type, value: v, tracks, actor, localTime, clipStart, shot, originalRoles = {}, los = "", direction = "" }: ContextInput): { attributes: Record<string, unknown>; sourceTimestamp?: number; frameNumber?: number } | null {
  if (!v.enabled || !PREDICTION_CLOCKS.includes(type)) return { attributes: {} };
  const available = numeric(v.available);
  if (clipStart == null || !Number.isFinite(clipStart) || clipStart < 0 || !Number.isFinite(localTime) || localTime < 0 || available == null || available < 0 || available > localTime) return null;
  const ids = new Map(tracks.map((track) => [track.id, track]));
  const unique = (values: string[]) => values.every((id) => ids.has(id)) && new Set(values).size === values.length;
  if (ids.size !== tracks.length || !v.defendersComplete || !v.defenders.length || !unique(v.defenders) || v.defenders.some((id) => !sideAllows(ids.get(id), "defense"))) return null;
  if (v.playDesign !== (type === "handoff" ? "rush" : "pass")) return null;
  const attrs: Record<string, unknown> = { feature_context_available_at_seconds: clipStart + available, feature_context_timebase: "recording",
    play_design: v.playDesign, defender_track_ids: v.defenders, defender_population_complete: true };
  const required = new Set(v.defenders);
  const offensive = (id: string) => ids.has(id) && !v.defenders.includes(id) && sideAllows(ids.get(id), "offense");
  if (type === "pass_release") {
    if (!v.forward || !offensive(v.qb) || v.qb !== actor || !offensive(v.target) || v.qb === v.target) return null;
    Object.assign(attrs, { forward_pass: true, qb_id: v.qb, intended_receiver_id: v.target });
    required.add(v.qb).add(v.target);
  } else if (type === "handoff" || type === "catch") {
    if (!offensive(v.possessor) || v.possessor !== actor || (type === "catch" && (!v.forward || !v.completed))) return null;
    Object.assign(attrs, { possessor_id: v.possessor, ...(type === "catch" ? { forward_pass: true, completion_observed: true, possession_side: "offense" } : {}) });
    required.add(v.possessor);
  } else {
    const selected = Object.entries(originalRoles).filter(([, id]) => id);
    const callsAt = numeric(v.callsAvailable), down = numeric(v.down), distance = numeric(v.distance), ballY = numeric(v.ballY), line = numeric(los);
    if (selected.length < 5 || selected.some(([role]) => ![...OL, "LTE", "RTE"].includes(role)) || OL.some((role) => !originalRoles[role]) || !unique(selected.map(([, id]) => id)) || selected.some(([, id]) => !offensive(id))
      || !v.frontComplete || !v.callsKnown || callsAt == null || callsAt < 0 || callsAt > localTime
      || down == null || !Number.isInteger(down) || down < 1 || down > 4 || distance == null || distance < 0
      || ballY == null || Math.abs(ballY) > 26.665 || line == null || line < 0 || line > 100 || !["1", "-1"].includes(direction)
      || !unique(v.rushers) || !unique(v.blitzers) || !unique(v.blockers) || !v.blockers.length
      || v.rushers.some((id) => !v.defenders.includes(id)) || v.blitzers.some((id) => !v.rushers.includes(id)) || v.blockers.some((id) => !offensive(id))
      || v.defenders.some((id) => !DEFENSE.includes(v.defensiveRoles[id])) || !v.defenders.some((id) => DL.includes(v.defensiveRoles[id])) || !v.defenders.some((id) => LB.includes(v.defensiveRoles[id]))) return null;
    const dl = v.defenders.filter((id) => DL.includes(v.defensiveRoles[id]));
    if (dl.some((id) => !["a", "b", "c", "edge"].includes(v.gaps[id]))) return null;
    Object.assign(attrs, { front_population_complete: true, ol_roles: Object.fromEntries(selected),
      defensive_roles: Object.fromEntries(v.defenders.map((id) => [id, v.defensiveRoles[id]])), dl_gap_alignment: Object.fromEntries(dl.map((id) => [id, v.gaps[id]])),
      call_context_known: true, call_context_available_at_seconds: clipStart + callsAt, call_context_timebase: "recording",
      called_rusher_ids: v.rushers, called_blitzer_ids: v.blitzers, protection_blocker_ids: v.blockers,
      down, distance_yards: distance, ball_y: ballY, los_x: line, attacking_direction: Number(direction) });
    selected.forEach(([, id]) => required.add(id)); v.blockers.forEach((id) => required.add(id));
  }
  const sourceTimestamp = clipStart + localTime, sourceShot = canonicalShotId(shot);
  if (sourceShot == null) return null;
  const frames: number[] = [];
  for (const id of required) {
    const points = ids.get(id)!.track_points.filter((point) => point.timestamp_seconds != null && Math.abs(point.timestamp_seconds - sourceTimestamp) < 1e-8);
    if (points.length !== 1 || !["observed", "visible"].includes(points[0].visibility_state ?? "") || canonicalShotId(points[0].shot_id) !== sourceShot
      || !Number.isSafeInteger(points[0].frame_number) || points[0].frame_number < 0) return null;
    frames.push(points[0].frame_number);
  }
  if (new Set(frames).size !== 1) return null;
  return { attributes: attrs, sourceTimestamp, frameNumber: frames[0] };
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) { return <div className="flex flex-col gap-1"><Label htmlFor={id} className="text-xs">{label}</Label>{children}</div>; }
export function PredictionContextFields({ type, value: v, onChange, tracks, actor, disabled, clipStart, clipEnd, onClockChange }: {
  type: string; value: PredictionContextDraft; onChange: (value: PredictionContextDraft) => void; tracks: OverlayTracklet[]; actor: string; disabled: boolean; clipStart?: number; clipEnd?: number; onClockChange: (localTime: number) => void;
}) {
  if (!PREDICTION_CLOCKS.includes(type)) return null;
  const options = tracks.map((track, index) => <option key={track.id} value={track.id}>T{index + 1}</option>);
  const selection = (key: "qb" | "target" | "possessor", label: string) => <Field id={`prediction-${key}`} label={label}><NativeSelect id={`prediction-${key}`} value={v[key]} disabled={disabled} onChange={(event) => onChange({ ...v, [key]: event.target.value })}><option value="">Unknown</option>{options}</NativeSelect></Field>;
  const check = (key: keyof PredictionContextDraft, label: string) => <label className="flex gap-2 text-xs"><input type="checkbox" checked={v[key] === true} disabled={disabled} onChange={(event) => onChange({ ...v, [key]: event.target.checked })} />{label}</label>;
  const list = (key: "defenders" | "rushers" | "blitzers" | "blockers", label: string, allowed = tracks) => <fieldset className="flex flex-col gap-1"><legend className="text-xs font-medium">{label}</legend>{allowed.map((track) => <label key={track.id} className="flex gap-2 text-xs"><input type="checkbox" checked={v[key].includes(track.id)} disabled={disabled} onChange={(event) => onChange({ ...v, [key]: event.target.checked ? [...v[key], track.id] : v[key].filter((id) => id !== track.id) })} />{label} T{tracks.indexOf(track) + 1}</label>)}</fieldset>;
  const text = (key: "available" | "callsAvailable" | "down" | "distance" | "ballY", label: string) => <Field id={`prediction-${key}`} label={label}><Input id={`prediction-${key}`} type="number" step={key === "down" ? "1" : "any"} value={v[key]} disabled={disabled} onChange={(event) => onChange({ ...v, [key]: event.target.value })} /></Field>;
  const actorPoints = tracks.find((track) => track.id === actor)?.track_points ?? [];
  const clocks = actorPoints.filter((point) => clipStart != null && clipEnd != null && point.timestamp_seconds != null && point.timestamp_seconds >= clipStart && point.timestamp_seconds <= clipEnd && canonicalShotId(point.shot_id) != null && ["observed", "visible"].includes(point.visibility_state ?? ""));
  return <section className="flex flex-col gap-2 rounded border border-border-soft p-2" aria-label="Prediction-time observation context">
    {check("enabled", "Add prediction-time context")}
    {v.enabled && <>
      <p className="text-xs text-muted-foreground">Review only information known at this event. These fields describe the observed play, never its later result. Player choices are source tracks, not roster assignments. All selected players need the same exact visible native frame; choose a source frame when playback is between frames.</p>
      {clipStart == null && <p role="status" className="text-xs">The original source window is unavailable. Context cannot be saved until this play is loaded from its recording.</p>}
      <Field id="prediction-source-frame" label="Prediction source frame"><NativeSelect id="prediction-source-frame" value="" disabled={disabled || !clocks.length} onChange={(event) => { if (event.target.value && clipStart != null) onClockChange(Number(event.target.value) - clipStart); }}><option value="">Choose an actual source frame…</option>{clocks.map((point) => <option key={`${point.frame_number}:${point.timestamp_seconds}`} value={point.timestamp_seconds!}>Play {(point.timestamp_seconds! - clipStart!).toFixed(6)}s · native frame {point.frame_number}</option>)}</NativeSelect></Field>
      {text("available", "Context available at (play seconds)")}
      <Field id="prediction-design" label="Observed play design"><NativeSelect id="prediction-design" value={v.playDesign} disabled={disabled} onChange={(event) => onChange({ ...v, playDesign: event.target.value })}><option value="">Unknown</option><option value="pass">Designed pass</option><option value="rush">Designed rush</option></NativeSelect></Field>
      {type === "pass_release" && <>{selection("qb", "Prediction quarterback track")}{selection("target", "Intended receiver track")}</>}
      {["handoff", "catch"].includes(type) && selection("possessor", "Prediction possessor track")}
      {["pass_release", "catch"].includes(type) && check("forward", "I observed an aimed forward pass to an offensive player")}
      {type === "catch" && check("completed", "I observed the offensive forward-pass completion at this catch")}
      {list("defenders", "Prediction defender")}
      {check("defendersComplete", "I reviewed the complete defender population at this clock")}
      {type === "snap" && <>
        <p className="text-xs text-muted-foreground">Use the original LT/LG/C/RG/RT tracks, measured LOS and attacking direction above. Optional observed LTE/RTE roles are retained; spacing uses the original five linemen. Pressure context needs explicit DL/LB roles and actual known calls. Unknown call lists cannot establish zero rushers or blitzers.</p>
        {tracks.filter((track) => v.defenders.includes(track.id)).map((track) => { const n = tracks.indexOf(track) + 1; return <div key={track.id} className="flex flex-col gap-2"><Field id={`prediction-role-${track.id}`} label={`Prediction role for T${n}`}><NativeSelect id={`prediction-role-${track.id}`} value={v.defensiveRoles[track.id] ?? ""} disabled={disabled} onChange={(event) => onChange({ ...v, defensiveRoles: { ...v.defensiveRoles, [track.id]: event.target.value } })}><option value="">Unknown</option>{DEFENSE.map((role) => <option key={role} value={role}>{role}</option>)}</NativeSelect></Field>{DL.includes(v.defensiveRoles[track.id]) && <Field id={`prediction-gap-${track.id}`} label={`Prediction DL gap for T${n}`}><NativeSelect id={`prediction-gap-${track.id}`} value={v.gaps[track.id] ?? ""} disabled={disabled} onChange={(event) => onChange({ ...v, gaps: { ...v.gaps, [track.id]: event.target.value } })}><option value="">Unknown</option>{["a", "b", "c", "edge"].map((gap) => <option key={gap} value={gap}>{gap.toUpperCase()}</option>)}</NativeSelect></Field>}</div>; })}
        {check("frontComplete", "I reviewed the complete original front at the snap")}
        {text("callsAvailable", "Call context available at (play seconds)")}
        {list("rushers", "Called rusher", tracks.filter((track) => v.defenders.includes(track.id)))}
        {list("blitzers", "Called blitzer", tracks.filter((track) => v.rushers.includes(track.id)))}
        {list("blockers", "Protection blocker", tracks.filter((track) => !v.defenders.includes(track.id)))}
        {check("callsKnown", "These rush, blitz and protection calls were known by the snap; unchecked players mean reviewed none")}
        {text("down", "Prediction down")}{text("distance", "Prediction distance (yards)")}{text("ballY", "Measured ball field y (yards)")}
      </>}
    </>}
  </section>;
}
