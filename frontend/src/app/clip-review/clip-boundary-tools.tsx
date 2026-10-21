"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAppState } from "@/lib/app-state";
import type { ApiClip } from "@/lib/types";
import { canSubmitCorrections } from "@/lib/roles";
import { fetchClipEdits, mergeClips, splitClip, undoClipEdit, type ClipEditOperation, type ClipEditResult } from "@/lib/clip-edits-api";
import { filmReviewHref } from "@/lib/analysis-context";
import { friendlyMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";

export function ClipBoundaryTools({ clip, siblings, readLocalTime, onChanged }: { clip: ApiClip; siblings: ApiClip[]; readLocalTime: () => number; onChanged: () => void }) {
  const { authToken, currentRole } = useAppState();
  const router = useRouter();
  const params = useSearchParams();
  const [at, setAt] = useState(((clip.start_time + clip.end_time) / 2).toFixed(3));
  const [otherId, setOtherId] = useState("");
  const [opened, setOpened] = useState(false);
  const [history, setHistory] = useState<ClipEditOperation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [revision, setRevision] = useState(0);
  const editable = canSubmitCorrections(currentRole);
  const ordered = [...new Map([clip, ...siblings].filter((item) => item.video_id === clip.video_id && item.provenance?.state !== "superseded").map((item) => [item.id, item])).values()]
    .sort((left, right) => left.start_time - right.start_time || left.id.localeCompare(right.id));
  const currentIndex = ordered.findIndex((item) => item.id === clip.id);
  const adjacent = [ordered[currentIndex - 1], ordered[currentIndex + 1]].filter((item): item is ApiClip => !!item && (item.start_time >= clip.start_time ? item.start_time - clip.end_time : clip.start_time - item.end_time) <= 30);
  const other = adjacent.find((item) => item.id === otherId);
  useEffect(() => {
    if (!opened || !editable) return;
    let cancelled = false;
    setHistoryError("");
    fetchClipEdits(clip.id, authToken).then((items) => { if (!cancelled) setHistory(items); }).catch((cause) => { if (!cancelled) setHistoryError(friendlyMessage(cause)); });
    return () => { cancelled = true; };
  }, [clip.id, authToken, opened, editable, revision]);
  async function perform(action: () => Promise<ClipEditResult>) {
    setBusy(true); setError("");
    try {
      const result = await action();
      onChanged();
      if (result.clips[0]) router.push(filmReviewHref(new URLSearchParams(params.toString()), result.clips[0].id));
      else setRevision((value) => value + 1);
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(false); }
  }
  if (!editable || clip.provenance?.state === "superseded") return null;
  return <details className="mt-4 border-t border-border-soft pt-3" onToggle={(event) => { if (event.currentTarget.open) setOpened(true); }}><summary className="cursor-pointer text-xs font-semibold">Split, merge and undo</summary>
    <p className="mt-2 text-xs text-muted-foreground">Edits preserve the original recording and history. New boundaries need reprocessing for dependent measurements.</p>
    <form className="mt-3 flex flex-col gap-2" onSubmit={(event) => { event.preventDefault(); void perform(() => splitClip(clip.id, Number(at), clip.annotation_revision ?? 0, authToken)); }}><label className="flex flex-col gap-1 text-xs text-muted-foreground">Split at source time (seconds)<Input type="number" required step="0.001" min={clip.start_time + 0.1} max={clip.end_time - 0.1} value={at} onChange={(event) => setAt(event.target.value)} /></label><div className="flex flex-wrap gap-2"><Button size="sm" type="button" variant="ghost" disabled={busy} onClick={() => setAt((clip.start_time + readLocalTime()).toFixed(3))}>Use playhead</Button><Button size="sm" disabled={busy || !Number.isFinite(Number(at)) || Number(at) < clip.start_time + 0.1 || Number(at) > clip.end_time - 0.1}>Split play</Button></div></form>
    <form className="mt-4 flex flex-col gap-2 border-t border-border-soft pt-3" onSubmit={(event) => { event.preventDefault(); if (other) void perform(() => mergeClips([clip.id, other.id], { [clip.id]: clip.annotation_revision ?? 0, [other.id]: other.annotation_revision ?? 0 }, authToken)); }}><label className="flex flex-col gap-1 text-xs text-muted-foreground">Merge with adjacent play<NativeSelect value={other?.id ?? ""} onChange={(event) => setOtherId(event.target.value)}><option value="">Choose an adjacent play</option>{adjacent.map((item) => <option key={item.id} value={item.id}>Play {item.play_number ?? "—"} · {item.start_time.toFixed(1)}–{item.end_time.toFixed(1)}s</option>)}</NativeSelect></label>{!adjacent.length ? <p className="text-xs text-muted-foreground">No adjacent play within 30 seconds in this recording.</p> : null}<Button size="sm" variant="outline" disabled={busy || !other}>Merge selected plays</Button></form>
    {history.length ? <div aria-label="Boundary edit history" className="mt-4 flex flex-col gap-3 border-t border-border-soft pt-3">{history.map((item) => <div key={item.operation_id} className="text-xs"><p className="capitalize">{item.operation_type}{item.created_at ? ` · ${new Date(item.created_at).toLocaleString()}` : ""}{item.state === "undone" ? " · Undone" : ""}</p>{item.after ? <p className="mt-1 tabular-nums text-muted-foreground">Result: {Object.values(item.after).map((window) => `${window.start_seconds.toFixed(2)}–${window.end_seconds.toFixed(2)}s`).join(", ")}</p> : null}{item.undo_available ? <Button size="sm" variant="outline" className="mt-2" disabled={busy} onClick={() => void perform(() => undoClipEdit(item.operation_id, item.revisions, authToken))}>Undo {item.operation_type}</Button> : item.undo_blocked_reason ? <p className="mt-1 text-muted-foreground">{item.undo_blocked_reason}</p> : null}</div>)}</div> : null}
    {error || historyError ? <p role="alert" className="mt-2 text-xs text-status-danger">{error || historyError}<Button size="sm" variant="link" onClick={() => { setError(""); onChanged(); setRevision((value) => value + 1); }}>Refresh edit history</Button></p> : null}
  </details>;
}
