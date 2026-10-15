"use client";

import { useCallback } from "react";
import { fetchCorrections } from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { useFetchState } from "@/lib/fetch-state";
import { Button } from "@/components/ui/button";

export function CorrectionHistory({ clipId, annotationRevision }: { clipId: string; annotationRevision?: number }) {
  const { authToken } = useAppState();
  const fetcher = useCallback(() => { void annotationRevision; return fetchCorrections(clipId, authToken); }, [clipId, authToken, annotationRevision]);
  const { state, reload } = useFetchState(fetcher);
  return <details className="mt-4 border-t border-border-soft pt-3">
    <summary className="cursor-pointer text-sm font-semibold">Correction history</summary>
    {state.kind === "loading" && <p role="status" className="mt-2 text-xs">Loading saved corrections…</p>}
    {state.kind === "error" && <p role="status" className="mt-2 text-xs text-status-warn">Could not load history. <Button size="sm" variant="link" onClick={reload}>Try again</Button></p>}
    {state.kind === "empty" && <p className="mt-2 text-xs text-muted-foreground">No corrections saved for this play.</p>}
    {state.kind === "ready" && <ul className="mt-2 space-y-3">{state.data.map((correction) => <li key={correction.id} className="text-xs"><p className="font-semibold">{correction.correction_type.replaceAll("_", " ")} · {new Date(correction.created_at).toLocaleString()}</p><p className="mt-1 text-muted-foreground">{Object.entries(correction.corrected_value).filter(([key]) => !key.endsWith("_id")).map(([key, value]) => `${key.replaceAll("_", " ")}: ${typeof value === "object" ? "updated" : String(value)}`).join(" · ")}</p>{correction.notes && <p className="mt-1">{correction.notes}</p>}</li>)}</ul>}
  </details>;
}
