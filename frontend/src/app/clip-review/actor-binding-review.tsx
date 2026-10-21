"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchActorBindingReview, removeInvalidReviewedEvent, updateReviewedEvent, type ActorBindingReview, type ActorBindingReviewEvent } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { canSubmitCorrections } from "@/lib/roles";
import { canonicalShotId } from "@/lib/shot-id";
import type { OverlayTracklet } from "@/lib/types";
import { Button } from "@/components/ui/button";

const SINGLES: Record<string, string> = { actor_id: "Observed player", qb_id: "Quarterback", carrier_id: "Ball carrier", ball_carrier_id: "Ball carrier", runner_id: "Runner", blocker_id: "Blocker", defender_id: "Defender", opponent_id: "Comparison player", ball_id: "Ball", center_id: "Center", player_track_id: "Player", receiver_id: "Receiver", rusher_id: "Rusher", kicker_id: "Kicker", tracklet_id: "Player", opponent_tracklet_id: "Opponent", intended_receiver_id: "Intended receiver", possessor_id: "Possessor" };
const LISTS = ["defender_track_ids", "called_rusher_ids", "called_blitzer_ids", "protection_blocker_ids", "front_track_ids"];
const MAPS = ["body_extents", "defensive_roles", "dl_gap_alignment"];
function object(value: unknown): Record<string, unknown> { return value != null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function references(attrs: Record<string, unknown>): [string, string][] {
  const found = new Map<string, string>();
  for (const [key, label] of Object.entries(SINGLES)) if (typeof attrs[key] === "string") found.set(attrs[key], label);
  for (const key of LISTS) if (Array.isArray(attrs[key])) attrs[key].forEach((id, i) => { if (typeof id === "string" && !found.has(id)) found.set(id, `${key.replaceAll("_", " ")} ${i + 1}`); });
  for (const [role, id] of Object.entries(object(attrs.ol_roles))) if (typeof id === "string") found.set(id, `Original ${role}`);
  for (const key of MAPS) for (const id of Object.keys(object(attrs[key]))) if (!found.has(id)) found.set(id, `Measured player ${found.size + 1}`);
  const occupancy = object(attrs.defender_occupancy);
  if (Array.isArray(occupancy.track_ids)) occupancy.track_ids.forEach((id, i) => { if (typeof id === "string" && !found.has(id)) found.set(id, `Observed defender ${i + 1}`); });
  return [...found];
}
function rewrite(attrs: Record<string, unknown>, map: Record<string, string>): Record<string, unknown> {
  const result = structuredClone(attrs);
  for (const key of Object.keys(SINGLES)) if (typeof result[key] === "string") result[key] = map[result[key]];
  for (const key of LISTS) if (Array.isArray(result[key])) result[key] = result[key].map((id) => typeof id === "string" ? map[id] : id);
  if (result.ol_roles) result.ol_roles = Object.fromEntries(Object.entries(object(result.ol_roles)).map(([role, id]) => [role, typeof id === "string" ? map[id] : id]));
  for (const key of MAPS) if (result[key]) result[key] = Object.fromEntries(Object.entries(object(result[key])).map(([id, value]) => [map[id], value]));
  const occupancy = object(result.defender_occupancy);
  if (Array.isArray(occupancy.track_ids)) result.defender_occupancy = { ...occupancy, track_ids: occupancy.track_ids.map((id) => typeof id === "string" ? map[id] : id) };
  return result;
}
const EXPLANATIONS: Record<string, string> = {
  actor_binding_observations_missing_or_changed: "The player observations changed or are no longer available.",
  actor_binding_observations_ambiguous: "More than one current track matches the saved player observations.",
  actor_binding_source_clip_or_window_mismatch: "The saved observation belongs to a different source or play window.",
  actor_binding_missing_or_protocol_unknown: "This older observation needs its players reviewed against the current film.",
  actor_binding_source_identity_unverified: "The recording identity has not yet been verified. Process the original recording before reviewing this observation.",
  actor_reference_not_active_in_clip: "A saved player track is no longer current in this play.",
  actor_reference_container_invalid: "Some saved player fields are malformed. Review the original observation fields before using this measurement.",
  actor_observations_incomplete: "The saved player observations lack a verified source clock, camera shot or image position.",
  prediction_clock_ambiguous: "More than one reviewed event defines this prediction clock. Review them on film; the prediction remains unavailable while the clock is ambiguous.",
};

export function ActorBindingReviewNotice({ clipId, annotationRevision, tracks, onSaved, clipStart, clipEnd }: { clipId: string; annotationRevision?: number; tracks: OverlayTracklet[]; onSaved?: () => void; clipStart: number; clipEnd: number }) {
  const { authToken, currentRole } = useAppState();
  const [review, setReview] = useState<(ActorBindingReview & { clipId: string }) | null>(null);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<{ event: ActorBindingReviewEvent; malformed: boolean; allowRemoval: boolean; clipId: string; revision: number } | null>(null);
  const generation = useRef(0);
  const activeScope = useRef({ clipId, mounted: true });
  activeScope.current.clipId = clipId;
  useEffect(() => { activeScope.current.mounted = true; return () => { activeScope.current.mounted = false; }; }, []);
  const canEdit = canSubmitCorrections(currentRole);
  const load = useCallback(async () => {
    const request = ++generation.current;
    try { const value = await fetchActorBindingReview(clipId, authToken); if (request === generation.current) { setReview({ ...value, clipId }); setError(false); } }
    catch { if (request === generation.current) setError(true); }
  }, [clipId, authToken]);
  useEffect(() => { void annotationRevision; if (canEdit) void load(); return () => { generation.current += 1; }; }, [load, annotationRevision, canEdit]);
  if (!canEdit) return null;
  if (error) return <div className="mt-3 text-xs" role="status">Player-reference review could not load. <Button variant="link" size="sm" onClick={() => void load()}>Retry review</Button></div>;
  if (review?.clipId !== clipId || (!review?.observations?.length && !review?.removed_observations?.length)) return null;
  return <section aria-label="Observations needing player review" className="mt-4 rounded border border-status-warn/40 p-3 text-sm">
    <h3 className="font-semibold">Review saved player observations</h3>
    <p className="mt-1 text-xs text-muted-foreground">Review the flagged source evidence before using affected measurements or predictions. The film remains available.</p>
    {Boolean(review.removed_observations?.length) && <div className="mt-3 rounded border border-border-soft p-2"><h4 className="font-medium">Removal history needs source review</h4><p className="mt-1 text-xs">The original tags remain removed. Changed, missing or ambiguous source evidence prevented matching them to new model proposals. Review the current film and any new tags before using measurements. The preserved removals are in Correction history.</p><ul className="mt-2 text-xs">{review.removed_observations!.map(({ event }, index) => <li key={event.id}>Removed observation {index + 1}: {event.event_type.replaceAll("_", " ")}{event.timestamp_seconds != null ? ` · ${event.timestamp_seconds.toFixed(1)}s` : ""}</li>)}</ul></div>}
    <ul className="mt-3 space-y-3">{review.observations.map(({ event, issues }, index) => <li key={event.id}>
      <p className="font-medium">Observation {index + 1}: {event.event_type.replaceAll("_", " ")}{event.timestamp_seconds != null ? ` · ${event.timestamp_seconds.toFixed(1)}s` : ""}</p>
      {[...new Set(issues.map((issue) => EXPLANATIONS[issue.reason] ?? "The saved source or player evidence needs staff review."))].map((message) => <p key={message} className="mt-1 text-xs">{message}</p>)}
      <Button size="sm" variant="outline" className="mt-2" onClick={() => setSelected({ event, clipId, revision: review.annotation_revision, malformed: issues.some((issue) => issue.reason === "actor_reference_container_invalid"), allowRemoval: issues.some((issue) => ["actor_reference_container_invalid", "prediction_clock_ambiguous"].includes(issue.reason)) })}>Review observation {index + 1}</Button>
    </li>)}</ul>
    {selected?.clipId === clipId && selected.revision === review.annotation_revision && <SavedObservationEditor key={`${selected.event.id}:${selected.revision}`} event={selected.event} malformed={selected.malformed} allowRemoval={selected.allowRemoval} revision={selected.revision} tracks={tracks} token={authToken} clipStart={clipStart} clipEnd={clipEnd} onDone={async () => { if (!activeScope.current.mounted || activeScope.current.clipId !== selected.clipId) return; setSelected(null); await load(); if (activeScope.current.mounted && activeScope.current.clipId === selected.clipId) onSaved?.(); }} />}
  </section>;
}

function SavedObservationEditor({ event, tracks, token, revision, malformed, allowRemoval, onDone, clipStart, clipEnd }: { event: ActorBindingReviewEvent; tracks: OverlayTracklet[]; token?: string; revision: number; malformed: boolean; allowRemoval: boolean; onDone: () => void; clipStart: number; clipEnd: number }) {
  const attrs = event.attributes ?? {};
  const refs = references(attrs);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [shot, setShot] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [confirmRemoval, setConfirmRemoval] = useState(false);
  const localTime = (value: unknown, clock: unknown) => typeof value === "number" && ["clip", "recording"].includes(String(clock)) ? String(value - (clock === "recording" ? clipStart : 0)) : "";
  const [when, setWhen] = useState(localTime(event.timestamp_seconds, attrs.timebase));
  const interval = attrs.start_seconds != null || attrs.end_seconds != null;
  const [first, setFirst] = useState(localTime(attrs.start_seconds ?? event.timestamp_seconds, attrs.interval_timebase ?? attrs.timebase));
  const [last, setLast] = useState(localTime(attrs.end_seconds, attrs.interval_timebase ?? attrs.timebase));
  const shots = [...new Set(tracks.flatMap((track) => track.track_points.map((point) => canonicalShotId(point.shot_id))).filter((value): value is string => value !== null))];
  const validTime = (value: string) => value.trim() !== "" && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= clipEnd - clipStart;
  const ready = !malformed && validTime(when) && (!interval || (validTime(first) && validTime(last) && Number(first) < Number(last))) && refs.every(([id]) => tracks.some((track) => track.id === mapping[id])) && new Set(Object.values(mapping)).size === refs.length && shots.includes(shot) && !saving;
  return <form className="mt-4 flex flex-col gap-3 border-t border-border-soft pt-3" onSubmit={async (e) => {
    e.preventDefault(); if (!ready) return;
    setSaving(true); setMessage("");
    try { await updateReviewedEvent(event.id, { ...rewrite(attrs, mapping), shot_id: shot, timebase: "clip", ...(interval ? { start_seconds: Number(first), end_seconds: Number(last), interval_timebase: "clip" } : {}) }, token, revision, Number(when)); onDone(); }
    catch { setMessage("Could not update this saved observation. Reload the play and try again."); }
    finally { setSaving(false); }
  }}>
    <p className="text-xs">Review the saved observation against the current film. Choose each actual player explicitly. Saving updates this observation and preserves its history.</p>
    {malformed && <p role="status" className="text-xs">The saved actor fields are invalid. Remove this observation, then use Fix something → Record position observation to enter reviewed fields. No measurement is created here.</p>}
    {allowRemoval && <div className="rounded border border-destructive/30 p-2"><p className="text-xs">Keep every valid observation. Remove only a tag you confirmed is incorrect or duplicated; several valid clock events can keep a prediction unavailable. The original and its removal remain in history.</p>{!confirmRemoval ? <Button type="button" variant="destructive" size="sm" disabled={saving} onClick={() => setConfirmRemoval(true)}>{malformed ? "Remove invalid observation" : "Remove saved observation"}</Button> : <div role="alertdialog" aria-label="Confirm observation removal" className="mt-2 flex flex-col gap-2"><p className="text-xs">Remove this saved {event.event_type.replaceAll("_", " ")} observation from current use?</p><div className="flex gap-2"><Button type="button" variant="destructive" size="sm" disabled={saving} onClick={async () => { setSaving(true); try { await removeInvalidReviewedEvent(event.id, token, revision); onDone(); } catch { setMessage("Could not remove this observation. Reload the play and try again."); } finally { setSaving(false); } }}>Confirm remove observation</Button><Button type="button" variant="outline" size="sm" disabled={saving} onClick={() => setConfirmRemoval(false)}>Keep observation</Button></div></div>}</div>}
    <label className="flex flex-col gap-1 text-xs">Reviewed play time (seconds)<input type="number" min="0" max={clipEnd - clipStart} step="0.001" value={when} onChange={(e) => setWhen(e.target.value)} className="rounded border bg-background p-2" /></label>
    {interval && <><label className="flex flex-col gap-1 text-xs">Reviewed interval start (play seconds)<input type="number" min="0" max={clipEnd - clipStart} step="0.001" value={first} onChange={(e) => setFirst(e.target.value)} className="rounded border bg-background p-2" /></label><label className="flex flex-col gap-1 text-xs">Reviewed interval end (play seconds)<input type="number" min="0" max={clipEnd - clipStart} step="0.001" value={last} onChange={(e) => setLast(e.target.value)} className="rounded border bg-background p-2" /></label></>}
    {refs.map(([id, label], index) => <label key={id} className="flex flex-col gap-1 text-xs">{label} · reference {index + 1}<select aria-label={`${label} reference ${index + 1}`} value={mapping[id] ?? ""} onChange={(e) => setMapping({ ...mapping, [id]: e.target.value })} className="rounded border bg-background p-2"><option value="">Choose current track</option>{tracks.map((track, n) => <option key={track.id} value={track.id}>Track {n + 1}{track.team_label ? ` · ${track.team_label}` : ""}{track.side_of_ball ? ` · ${track.side_of_ball}` : ""}</option>)}</select></label>)}
    <label className="flex flex-col gap-1 text-xs">Reviewed source shot<select aria-label="Reviewed source shot" value={shot} onChange={(e) => setShot(e.target.value)} className="rounded border bg-background p-2"><option value="">Choose source shot</option>{shots.map((value, n) => <option key={value} value={value}>Source shot {n + 1}</option>)}</select></label>
    {tracks.length === 0 && <p className="text-xs">No current player tracks are available. Process the original recording, then return to this observation.</p>}
    {message && <p role="alert" className="text-xs">{message}</p>}
    <Button type="submit" size="sm" disabled={!ready}>{saving ? "Saving…" : "Update saved observation"}</Button>
  </form>;
}
