"use client";

import { useState } from "react";
import type { ApiClip, OurPossession } from "@/lib/types";
import { updateClip } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { canSubmitCorrections } from "@/lib/roles";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

/** Edits coaching truth now; model training is a separate consequence. */
export function ReviewMetadataPanel({ clip, onSaved, onNext }: {
  clip: ApiClip;
  onSaved: () => void;
  onNext: (() => void) | null;
}) {
  const { authToken, currentRole, refreshData } = useAppState();
  const [personnel, setPersonnel] = useState(clip.personnel_grouping ?? "");
  const [down, setDown] = useState(clip.down == null ? "" : String(clip.down));
  const [distance, setDistance] = useState(clip.distance == null ? "" : String(clip.distance));
  const [zone, setZone] = useState(clip.field_zone ?? "");
  const [possession, setPossession] = useState(clip.our_possession ?? "");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const locked = !canSubmitCorrections(currentRole);

  const save = async (reviewed: boolean, next: boolean, clearTracking = false) => {
    if (saving || locked) return;
    if (distance !== "" && (!Number.isFinite(Number(distance)) || Number(distance) < 0)) {
      setError("Distance must be zero or more yards.");
      return;
    }
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      await updateClip(clip.id, {
        expected_revision: clip.annotation_revision,
        personnel_grouping: personnel.trim() || null,
        down: down ? Number(down) : null,
        distance: distance === "" ? null : Number(distance),
        field_zone: zone.trim() || null,
        our_possession: (possession || null) as OurPossession | null,
        ...(reviewed ? { is_reviewed: true } : {}),
        ...(clearTracking ? { tracking_review_required: false } : {}),
      }, authToken);
      setMessage(clearTracking ? "Tracking flag cleared. Reprocess to restore measurements." : reviewed ? "Play marked reviewed." : "Play tags saved.");
      onSaved();
      void refreshData();
      if (next) onNext?.();
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : String(cause);
      setError(detail.includes("409") ? "This play changed since you opened it. Reload its latest tags before saving again." : detail);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="mt-4 border-t border-border-soft pt-4" aria-label="Play tags and review">
      <h3 className="text-sm font-semibold">Play tags</h3>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div className="col-span-2 grid gap-1"><Label htmlFor="play-possession">Toledo side</Label>
          <NativeSelect id="play-possession" value={possession} onChange={(event) => setPossession(event.target.value as OurPossession | "")} disabled={locked || saving}>
            <option value="">Use recording default</option><option value="offense">Offense</option><option value="defense">Defense</option><option value="special_teams">Special teams</option>
          </NativeSelect>
        </div>
        <div className="grid gap-1"><Label htmlFor="play-personnel">Personnel</Label><Input id="play-personnel" value={personnel} maxLength={20} placeholder="e.g. 11" onChange={(event) => setPersonnel(event.target.value)} disabled={locked || saving} /></div>
        <div className="grid gap-1"><Label htmlFor="play-down">Down</Label><NativeSelect id="play-down" value={down} onChange={(event) => setDown(event.target.value)} disabled={locked || saving}><option value="">Unknown</option>{[1, 2, 3, 4].map((value) => <option key={value} value={value}>{value}</option>)}</NativeSelect></div>
        <div className="grid gap-1"><Label htmlFor="play-distance">Distance (yards)</Label><Input id="play-distance" type="number" min={0} step="any" value={distance} onChange={(event) => setDistance(event.target.value)} disabled={locked || saving} /></div>
        <div className="grid gap-1"><Label htmlFor="play-zone">Field zone</Label><Input id="play-zone" value={zone} maxLength={30} placeholder="e.g. red zone" onChange={(event) => setZone(event.target.value)} disabled={locked || saving} /></div>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={locked || saving} onClick={() => void save(false, false)}>Save tags</Button>
        <Button size="sm" disabled={locked || saving} onClick={() => void save(true, false)}>{saving ? "Saving…" : "Mark reviewed"}</Button>
        {onNext && <Button size="sm" variant="outline" disabled={locked || saving} onClick={() => void save(true, true)}>Save and next</Button>}
        {clip.tracking_review_required && <Button size="sm" variant="outline" disabled={locked || saving} onClick={() => void save(false, false, true)}>Clear tracking review flag</Button>}
      </div>
      {locked && <p className="mt-2 text-xs text-muted-foreground">A coach or analyst can edit tags and complete review.</p>}
      {message && <p role="status" className="mt-2 text-xs text-status-ok">{message}</p>}
      {error && <div role="alert" className="mt-2 text-xs text-status-danger">{error}<Button size="sm" variant="link" onClick={onSaved}>Reload play</Button></div>}
    </section>
  );
}
