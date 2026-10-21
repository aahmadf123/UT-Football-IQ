"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

export const OUTCOME_TYPES = ["pass_target", "pass_result", "reception", "possession_end", "carrier_cut", "tackle_outcome"];
const POSSESSION_STARTS = ["handoff", "possession_start"];
export type OutcomeDraft = { possession: string; outcome: string; contested: string; responsibility: boolean; confirmed: boolean; forced: boolean };
export const emptyOutcomeDraft = (): OutcomeDraft => ({ possession: "", outcome: "", contested: "", responsibility: false, confirmed: false, forced: false });

export function outcomeAttributes(type: string, value: OutcomeDraft, opponent: string): Record<string, unknown> | null {
  if (POSSESSION_STARTS.includes(type)) return value.possession.trim() ? { possession_id: value.possession.trim() } : {};
  if (!OUTCOME_TYPES.includes(type)) return {};
  if (!value.possession.trim()) return null;
  const attrs: Record<string, unknown> = { possession_id: value.possession.trim() };
  if (type === "pass_target") {
    if (!value.confirmed || value.responsibility && !opponent) return null;
    Object.assign(attrs, { forward_pass: true, ...(opponent ? { opponent_id: opponent } : {}), responsibility_confirmed: value.responsibility });
  } else if (type === "pass_result") {
    if (!["complete", "incomplete", "interception"].includes(value.outcome)) return null;
    Object.assign(attrs, { outcome: value.outcome });
    if (value.contested) attrs.contested = value.contested === "yes";
  } else if (type === "reception") {
    if (!value.confirmed) return null;
    Object.assign(attrs, { forward_pass: true, completion_observed: true, possession_side: "offense" });
  } else if (type === "possession_end") {
    if (!["tackle", "out_of_bounds", "touchdown", "fumble", "dead_ball"].includes(value.outcome)) return null;
    Object.assign(attrs, { possession_ended: true, terminal_reason: value.outcome });
  } else if (type === "carrier_cut") {
    if (!value.confirmed) return null;
    attrs.cut_observed = true;
  } else {
    if (!value.confirmed || !opponent || !["made", "missed", "assist"].includes(value.outcome)) return null;
    Object.assign(attrs, { opponent_id: opponent, outcome: value.outcome, tackle_observed: true });
    if (value.outcome === "missed") attrs.forced_by_carrier = value.forced;
  }
  return attrs;
}

export function ObservedOutcomesFields({ type, value, onChange, disabled }: { type: string; value: OutcomeDraft; onChange: (value: OutcomeDraft) => void; disabled: boolean }) {
  if (!OUTCOME_TYPES.includes(type) && !POSSESSION_STARTS.includes(type)) return null;
  const choices = type === "pass_result" ? ["complete", "incomplete", "interception"] : type === "possession_end" ? ["tackle", "out_of_bounds", "touchdown", "fumble", "dead_ball"] : type === "tackle_outcome" ? ["made", "missed", "assist"] : [];
  const confirmation = type === "pass_target" ? "I observed an aimed forward-pass target for this player" : type === "reception" ? "I observed this offensive player complete a forward-pass reception" : type === "carrier_cut" ? "I observed a cut by the player in possession" : type === "tackle_outcome" ? "I reviewed this defender's tackle attempt against the selected carrier" : null;
  return <>
    <p className="text-xs text-muted-foreground">Reuse a possession name across its target, reception, cut and endpoint. Select the receiver/carrier as actor; for a tackle outcome select the defender as actor and carrier as opponent. Responsibility and results require reviewed film.</p>
    <Label htmlFor="outcome-possession">Possession name</Label><Input id="outcome-possession" value={value.possession} disabled={disabled} onChange={(event) => onChange({ ...value, possession: event.target.value })} />
    {choices.length > 0 && <><Label htmlFor="observed-outcome">Reviewed outcome</Label><NativeSelect id="observed-outcome" value={value.outcome} disabled={disabled} onChange={(event) => onChange({ ...value, outcome: event.target.value })}><option value="">Unknown</option>{choices.map((choice) => <option key={choice} value={choice}>{choice.replaceAll("_", " ")}</option>)}</NativeSelect></>}
    {confirmation && <label className="flex gap-2 text-xs"><input type="checkbox" checked={value.confirmed} disabled={disabled} onChange={(event) => onChange({ ...value, confirmed: event.target.checked })} />{confirmation}</label>}
    {type === "pass_target" && <label className="flex gap-2 text-xs"><input type="checkbox" checked={value.responsibility} disabled={disabled} onChange={(event) => onChange({ ...value, responsibility: event.target.checked })} />The selected defender had reviewed responsibility for this target</label>}
    {type === "pass_result" && <><Label htmlFor="outcome-contest">Reviewed contest</Label><NativeSelect id="outcome-contest" value={value.contested} disabled={disabled} onChange={(event) => onChange({ ...value, contested: event.target.value })}><option value="">Unknown</option><option value="yes">Contested</option><option value="no">Uncontested</option></NativeSelect></>}
    {type === "tackle_outcome" && value.outcome === "missed" && <label className="flex gap-2 text-xs"><input type="checkbox" checked={value.forced} disabled={disabled} onChange={(event) => onChange({ ...value, forced: event.target.checked })} />Film review attributes the missed tackle to the carrier</label>}
  </>;
}
