"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAppState } from "@/lib/app-state";
import { fetchSavedViews, saveAnalysisView, type SavedAnalysisView } from "@/lib/analytics-api";
import { friendlyMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";

export function SavedFilmViews({ readPlayback, onRestore }: { readPlayback: () => { time: number; rate: number }; onRestore?: (params: URLSearchParams) => void }) {
  const { authToken } = useAppState();
  const params = useSearchParams();
  const router = useRouter();
  const [views, setViews] = useState<SavedAnalysisView[]>([]);
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"private" | "team">("private");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [opened, setOpened] = useState(false);
  useEffect(() => {
    if (!opened) return;
    let cancelled = false;
    fetchSavedViews(authToken).then((items) => { if (!cancelled) { setViews(items.filter((item) => item.surface === "film")); setError(""); } }).catch((cause) => { if (!cancelled) setError(friendlyMessage(cause)); });
    return () => { cancelled = true; };
  }, [authToken, revision, opened]);
  async function save(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const playback = readPlayback();
      const filters = Object.fromEntries(params.entries());
      filters.t = String(playback.time); filters.rate = String(playback.rate);
      const item = await saveAnalysisView({ name: name.trim(), surface: "film", visibility, filters }, authToken);
      setViews((current) => [...current, item]); setName("");
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(false); }
  }
  return <details className="rounded border border-border-soft px-3 py-2 text-sm" onToggle={(event) => { if (event.currentTarget.open) setOpened(true); }}><summary className="cursor-pointer font-medium">Saved film workspaces</summary>
    <div className="mt-3 flex flex-wrap gap-3"><label className="flex min-w-48 flex-1 flex-col gap-1 text-xs text-muted-foreground">Open workspace<NativeSelect defaultValue="" onChange={(event) => {
      const item = views.find((view) => view.id === event.target.value);
      if (!item) return;
      const next = new URLSearchParams();
      // Persisted filter data is untrusted; only scalar query values travel.
      for (const [key, value] of Object.entries(item.filters)) if (typeof value === "string") next.set(key, value);
      if (next.has("clipId") || next.has("videoId")) { onRestore?.(next); router.push(`/clip-review/?${next}`); event.target.value = ""; }
      else setError("This saved workspace has no selected film. Open a play and save it again.");
    }}><option value="">Choose a saved workspace</option>{views.map((view) => <option value={view.id} key={view.id}>{view.name} · {view.visibility}</option>)}</NativeSelect></label></div>
    <form onSubmit={save} className="mt-3 flex flex-wrap items-end gap-3"><label className="flex min-w-48 flex-1 flex-col gap-1 text-xs text-muted-foreground">Workspace name<Input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} placeholder="Trench study · coach layout" /></label><label className="flex flex-col gap-1 text-xs text-muted-foreground">Visibility<NativeSelect value={visibility} onChange={(event) => setVisibility(event.target.value as "private" | "team")}><option value="private">Only me</option><option value="team">Team staff</option></NativeSelect></label><Button size="sm" disabled={busy || !name.trim()}>{busy ? "Saving…" : "Save film workspace"}</Button></form>
    {error ? <p role="alert" className="mt-2 text-xs text-status-danger">{error}<Button variant="link" size="sm" onClick={() => setRevision((value) => value + 1)}>Retry</Button></p> : null}
  </details>;
}
