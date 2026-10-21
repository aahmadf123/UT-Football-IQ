"use client";

import type { OverlayTracklet } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

type BodyDraft = { track: string; min_x: string; max_x: string; min_y: string; max_y: string };
export type EvidenceDraft = {
  pressureState: string; intervalEnd: string; coverageComplete: boolean;
  qb: string; originalRoles: Record<string, string>;
  bodies: BodyDraft[]; occupancyComplete: boolean; defenders: string[];
};
const emptyBody = (): BodyDraft => ({ track: "", min_x: "", max_x: "", min_y: "", max_y: "" });
export const emptyEvidenceDraft = (): EvidenceDraft => ({ pressureState: "", intervalEnd: "", coverageComplete: false,
  qb: "", originalRoles: {}, bodies: [emptyBody()], occupancyComplete: false, defenders: [] });
export const INTERACTION_TYPES = ["engagement_start", "engagement_end", "chip_end", "route_start"];
export const CARRIER_TYPES = ["handoff", "possession_start", "first_contact", "end_of_play", "tackle", "out_of_bounds", "touchdown"];
export const BODY_TYPES = ["geometry_observation", "snap"];
const ROLES = ["LT", "LG", "C", "RG", "RT", "LTE", "RTE"];

export function buildEvidenceAttributes(type: string, actor: string, time: number, draft: EvidenceDraft,
  tracks: OverlayTracklet[], opponent: string, interaction: string, duration?: number): Record<string, unknown> | null {
  const ids = new Set(tracks.map((track) => track.id));
  const result: Record<string, unknown> = {};
  if (type === "pressure_observation") {
    const end = Number(draft.intervalEnd);
    if (!ids.has(actor) || !["none", "pressure"].includes(draft.pressureState) || !draft.intervalEnd.trim() || !Number.isFinite(end) || end <= time || (duration != null && end > duration)) return null;
    Object.assign(result, { qb_id: actor, pressure_state: draft.pressureState, start_seconds: time,
      end_seconds: end, interval_timebase: "clip", coverage_complete: draft.coverageComplete });
  }
  if (CARRIER_TYPES.includes(type)) {
    if (!ids.has(actor)) return null;
    result.carrier_id = actor;
    if (type === "first_contact") result.first_contact = true;
  }
  if (INTERACTION_TYPES.includes(type)) {
    if (!ids.has(actor) || !interaction.trim() || !ids.has(opponent) || opponent === actor) return null;
    Object.assign(result, { blocker_id: actor, defender_id: opponent, interaction_id: interaction.trim() });
  }
  if (type === "snap") {
    const selected = Object.entries(draft.originalRoles).filter(([, id]) => id);
    if (selected.length) {
      if (["LT", "LG", "C", "RG", "RT"].some((role) => !draft.originalRoles[role]) || selected.some(([, id]) => !ids.has(id)) || new Set(selected.map(([, id]) => id)).size !== selected.length) return null;
      result.ol_roles = Object.fromEntries(selected);
      result.center_id = draft.originalRoles.C;
    }
    if (draft.qb) {
      if (!ids.has(draft.qb) || selected.some(([, id]) => id === draft.qb)) return null;
      result.qb_id = draft.qb;
    }
  }
  if (BODY_TYPES.includes(type)) {
    const selected = draft.bodies.filter((body) => Object.values(body).some((value) => value.trim()));
    if (type === "geometry_observation" && !selected.length) return null;
    if (selected.length || draft.occupancyComplete || draft.defenders.length) {
      const bodies: Record<string, Record<string, number>> = {};
      for (const body of selected) {
        const { track, ...coordinates } = body;
        if (!ids.has(track) || bodies[track] || Object.values(coordinates).some((value) => !value.trim() || !Number.isFinite(Number(value)))) return null;
        const bounds = Object.fromEntries(Object.entries(coordinates).map(([key, value]) => [key, Number(value)]));
        if (!(bounds.min_x < bounds.max_x && bounds.min_y < bounds.max_y) || bounds.min_x < -10 || bounds.max_x > 110 || bounds.min_y < -26.665 || bounds.max_y > 26.665) return null;
        bodies[track] = bounds;
      }
      if (draft.defenders.some((id) => !ids.has(id) || !bodies[id])) return null;
      Object.assign(result, { body_extents: bodies, defender_occupancy: { complete: draft.occupancyComplete, track_ids: draft.defenders } });
    }
  }
  return result;
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return <div className="flex flex-col gap-1"><Label htmlFor={id} className="text-xs">{label}</Label>{children}</div>;
}

export function ObservedEvidenceFields({ type, tracks, value, onChange, disabled }: {
  type: string; tracks: OverlayTracklet[]; value: EvidenceDraft; onChange: (value: EvidenceDraft) => void; disabled: boolean;
}) {
  const options = tracks.map((track, index) => <option key={track.id} value={track.id}>T{index + 1}</option>);
  return <>
    {type === "pressure_observation" && <>
      <p className="text-xs text-muted-foreground">The selected actor is the observed QB. Continuous negative coverage must be explicitly reviewed; unknown intervals cannot establish first pressure.</p>
      <Field id="pressure-state" label="Observed pressure state"><NativeSelect id="pressure-state" value={value.pressureState} disabled={disabled} onChange={(event) => onChange({ ...value, pressureState: event.target.value })}><option value="">Select observed state…</option><option value="none">No pressure observed</option><option value="pressure">Pressure observed</option></NativeSelect></Field>
      <Field id="pressure-end" label="Pressure interval end (play seconds)"><Input id="pressure-end" type="number" min={0} step="0.01" value={value.intervalEnd} disabled={disabled} onChange={(event) => onChange({ ...value, intervalEnd: event.target.value })} /></Field>
      <label className="flex gap-2 text-xs"><input type="checkbox" checked={value.coverageComplete} disabled={disabled} onChange={(event) => onChange({ ...value, coverageComplete: event.target.checked })} />Pressure state was observed continuously for this entire interval</label>
    </>}
    {type === "snap" && <>
      <p className="text-xs text-muted-foreground">Original roles stay bound to these tracks when players pull or cross. Leave the map empty when unknown.</p>
      {ROLES.map((role) => <Field key={role} id={`original-${role}`} label={`Original ${role} track${["LTE", "RTE"].includes(role) ? " (optional)" : ""}`}><NativeSelect id={`original-${role}`} value={value.originalRoles[role] ?? ""} disabled={disabled} onChange={(event) => onChange({ ...value, originalRoles: { ...value.originalRoles, [role]: event.target.value } })}><option value="">Unknown</option>{options}</NativeSelect></Field>)}
      <Field id="original-qb" label="Original QB track (optional)"><NativeSelect id="original-qb" value={value.qb} disabled={disabled} onChange={(event) => onChange({ ...value, qb: event.target.value })}><option value="">Unknown</option>{options}</NativeSelect></Field>
    </>}
    {BODY_TYPES.includes(type) && <>
      <p className="text-xs text-muted-foreground">Supply measured field-coordinate body footprints. Image boxes do not provide body width. A clear lane also needs all original blockers and the complete observed defender population, including deep defenders. List each observed defender with a measured footprint; the measurement checks which footprints intersect the LOS.</p>
      {value.bodies.map((body, index) => <fieldset key={index} className="rounded border border-border-soft p-2"><legend className="text-xs">Measured body {index + 1}</legend>
        <Field id={`body-${index}-track`} label={`Body ${index + 1} observed track`}><NativeSelect id={`body-${index}-track`} value={body.track} disabled={disabled} onChange={(event) => onChange({ ...value, bodies: value.bodies.map((item, i) => i === index ? { ...item, track: event.target.value } : item) })}><option value="">Select a track…</option>{options}</NativeSelect></Field>
        <div className="grid grid-cols-2 gap-2">{(["min_x", "max_x", "min_y", "max_y"] as const).map((key) => <Field key={key} id={`body-${index}-${key}`} label={`Body ${index + 1} measured ${key.replace("_", " ")} (yards)`}><Input id={`body-${index}-${key}`} type="number" step="0.01" value={body[key]} disabled={disabled} onChange={(event) => onChange({ ...value, bodies: value.bodies.map((item, i) => i === index ? { ...item, [key]: event.target.value } : item) })} /></Field>)}</div>
      </fieldset>)}
      <div className="flex gap-2"><Button type="button" variant="outline" size="sm" disabled={disabled || value.bodies.length >= 24} onClick={() => onChange({ ...value, bodies: [...value.bodies, emptyBody()] })}>Add measured body</Button><Button type="button" variant="outline" size="sm" disabled={disabled || value.bodies.length <= 1} onClick={() => onChange({ ...value, bodies: value.bodies.slice(0, -1) })}>Remove last body</Button></div>
      <fieldset className="flex flex-col gap-1"><legend className="text-xs">All observed defenders, including those away from the LOS</legend>{tracks.map((track,index) => <label key={track.id} className="flex gap-2 text-xs"><input type="checkbox" checked={value.defenders.includes(track.id)} disabled={disabled} onChange={(event) => onChange({ ...value, defenders: event.target.checked ? [...value.defenders, track.id] : value.defenders.filter((id) => id !== track.id) })} />Defender T{index + 1}</label>)}</fieldset>
      <label className="flex gap-2 text-xs"><input type="checkbox" checked={value.occupancyComplete} disabled={disabled} onChange={(event) => onChange({ ...value, occupancyComplete: event.target.checked })} />The complete observed defender population is listed with measured footprints</label>
    </>}
    {CARRIER_TYPES.includes(type) && <p className="text-xs text-muted-foreground">The selected track is the explicitly observed carrier. Endpoint and contact locations use its supported calibrated source track at this time; hidden positions remain unavailable.</p>}
    {INTERACTION_TYPES.includes(type) && <p className="text-xs text-muted-foreground">Select the observed blocker and defender, and reuse their interaction name for the matching endpoint. Closeness cannot establish an interaction.</p>}
  </>;
}
