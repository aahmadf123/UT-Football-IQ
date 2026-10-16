"use client";

import { useEffect, useRef, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { createPlayer, fetchPlayer, importPlayers, updatePlayer, type PlayerCreate, type PlayerImportResult } from "@/lib/api";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { Switch } from "@/components/ui/switch";
import { friendlyMessage } from "@/lib/api-error";

const GROUPS = ["QB", "Skill", "OL", "DL", "LB", "DB", "ST"] as const;

export function RosterEditor({ playerId, onClose }: { playerId: string | null; onClose: () => void }) {
  const { authToken, refreshData } = useAppState();
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [jersey, setJersey] = useState("");
  const [position, setPosition] = useState("");
  const [group, setGroup] = useState("");
  const [active, setActive] = useState(true);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFirstName(""); setLastName(""); setJersey(""); setPosition(""); setGroup(""); setActive(true); setError(null);
    if (!playerId) { setLoading(false); return; }
    setLoading(true);
    fetchPlayer(playerId, authToken).then((player) => {
      if (cancelled) return;
      setFirstName(player.first_name); setLastName(player.last_name); setJersey(player.jersey_number === null ? "" : String(player.jersey_number)); setPosition(player.position ?? ""); setGroup(player.position_group ?? ""); setActive(player.is_active);
    }).catch((cause) => { if (!cancelled) setError(friendlyMessage(cause)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [playerId, authToken]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const number = jersey === "" ? null : Number(jersey);
    if (number !== null && (!Number.isInteger(number) || number < 0 || number > 99)) { setError("Jersey numbers must be whole numbers from 0 to 99."); return; }
    setSaving(true); setError(null);
    try {
      const body: PlayerCreate = { first_name: firstName.trim(), last_name: lastName.trim(), jersey_number: number, position: position.trim() || null, position_group: group ? group as PlayerCreate["position_group"] : null, is_active: active };
      if (playerId) await updatePlayer(playerId, body, authToken); else await createPlayer(body, authToken);
      await refreshData();
      onClose();
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setSaving(false); }
  }

  return <Dialog open={playerId !== null} onOpenChange={(open) => { if (!open && !saving) onClose(); }}><DialogContent className="sm:max-w-xl"><DialogHeader><DialogTitle>{playerId ? "Edit player" : "Add player"}</DialogTitle><DialogDescription>{playerId ? "Update roster details while keeping this player’s film and corrections linked." : "Add a player to your local roster so verified tracks can be linked to their profile."}</DialogDescription></DialogHeader>
    <form onSubmit={save} className="flex flex-col gap-4">
      {loading ? <p role="status">Loading player…</p> : null}
      {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
      <fieldset disabled={loading || saving} className="grid gap-4 sm:grid-cols-2">
        <div><Label htmlFor="player-first-name">First name</Label><Input id="player-first-name" required value={firstName} onChange={(event) => setFirstName(event.target.value)} maxLength={100} className="mt-2" /></div>
        <div><Label htmlFor="player-last-name">Last name</Label><Input id="player-last-name" required value={lastName} onChange={(event) => setLastName(event.target.value)} maxLength={100} className="mt-2" /></div>
        <div><Label htmlFor="player-jersey-number">Jersey number</Label><Input id="player-jersey-number" type="number" min={0} max={99} value={jersey} onChange={(event) => setJersey(event.target.value)} className="mt-2" /></div>
        <div><Label htmlFor="player-position">Position</Label><Input id="player-position" value={position} onChange={(event) => setPosition(event.target.value)} placeholder="WR, QB, RB…" maxLength={40} className="mt-2" /></div>
        <div><Label htmlFor="player-position-group">Coaching group</Label><NativeSelect id="player-position-group" value={group} onChange={(event) => setGroup(event.target.value)} className="mt-2"><option value="">Unassigned</option>{GROUPS.map((value) => <option key={value} value={value}>{value}</option>)}</NativeSelect></div>
        <div className="flex items-center justify-between gap-3 rounded-lg border border-border-soft p-3"><Label htmlFor="player-active-roster">On the active roster</Label><Switch id="player-active-roster" checked={active} onCheckedChange={setActive} /></div>
      </fieldset>
      <DialogFooter><Button type="button" variant="outline" onClick={onClose} disabled={saving}>Cancel</Button><Button disabled={saving || loading || !firstName.trim() || !lastName.trim()}>{saving ? "Saving…" : "Save player"}</Button></DialogFooter>
    </form>
  </DialogContent></Dialog>;
}

export function RosterImport({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { authToken, refreshData } = useAppState();
  const [csv, setCsv] = useState("");
  const [preview, setPreview] = useState<PlayerImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  function change(value: string) { setCsv(value); setPreview(null); setError(null); setFinished(false); }

  async function inspect(apply: boolean) {
    setBusy(true); setError(null);
    try {
      const result = await importPlayers(csv, !apply, authToken);
      setPreview(result);
      if (apply) { setFinished(true); await refreshData(); }
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(false); }
  }

  return <Dialog open={open} onOpenChange={(value) => { if (!value && !busy) onClose(); }}><DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl"><DialogHeader><DialogTitle>Import roster</DialogTitle><DialogDescription>Preview the CSV before adding players. Existing players are matched so their film associations stay intact.</DialogDescription></DialogHeader>
    <div className="flex flex-col gap-4"><div className="flex flex-wrap items-center gap-3"><Button variant="outline" onClick={() => fileInput.current?.click()} disabled={busy}>Choose CSV file</Button><input ref={fileInput} type="file" accept=".csv,text/csv" className="hidden" aria-label="Roster CSV file" onChange={async (event) => { const file = event.target.files?.[0]; if (file) change(await file.text()); event.target.value = ""; }} /><p className="text-xs text-muted-foreground">Required columns: first_name, last_name</p></div>
      <Label htmlFor="roster-csv">CSV contents</Label><textarea id="roster-csv" value={csv} onChange={(event) => change(event.target.value)} disabled={busy} rows={7} className="w-full rounded-lg border border-input bg-transparent p-3 font-mono text-xs focus-visible:outline-ring" placeholder={"first_name,last_name,jersey_number,position,position_group\nJordan,Smith,12,QB,QB"} />
      <p className="text-sm text-muted-foreground">Optional columns: jersey_number, position, position_group, id, is_active. Correct conflicting rows in the CSV or edit individual players in the roster.</p>
      {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
      {preview ? <div className="rounded-lg border border-border-soft p-4"><p role="status" className="font-medium">{finished ? "Import complete" : "Preview"}: {preview.created} {finished ? "added" : "new players"} · {preview.skipped} already present · {preview.errors.length} errors</p><div className="mt-3 max-h-64 overflow-y-auto space-y-2 text-sm">{preview.rows.map((row) => <div key={row.line} className={row.error ? "text-status-danger" : "text-muted-foreground"}>Line {row.line}: {row.status}{row.error ? ` · ${row.error}` : ""}</div>)}</div></div> : null}
      <DialogFooter><Button variant="outline" onClick={onClose} disabled={busy}>{finished ? "Done" : "Cancel"}</Button><Button variant="outline" disabled={busy || !csv.trim()} onClick={() => void inspect(false)}>{busy ? "Checking…" : "Preview roster"}</Button>{preview && !finished ? <Button disabled={busy || preview.errors.length > 0 || preview.created === 0} onClick={() => void inspect(true)}>Import {preview.created} players</Button> : null}</DialogFooter>
    </div>
  </DialogContent></Dialog>;
}
