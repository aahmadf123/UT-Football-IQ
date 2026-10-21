"use client";

import { useEffect, useState } from "react";
import { fetchSessions } from "@/lib/api";
import type { AnalysisContext } from "@/lib/analysis-context";
import type { ApiSession } from "@/lib/types";
import { friendlyMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const fields = [
  ["personnel", "Personnel", "e.g. 11", 20],
  ["formation", "Formation", "All formations", 100],
  ["situation", "Situation", "All situations", 100],
  ["drill_type", "Drill type", "All drills", 100],
  ["contact_level", "Contact level", "All contact levels", 100],
  ["opponent", "Opponent", "All opponents", 120],
] as const;

export function AnalysisCohortFilters({ context, onChange, token }: {
  context: AnalysisContext; onChange: (patch: Partial<AnalysisContext>) => void; token?: string;
}) {
  const [opened, setOpened] = useState(false);
  const [sessions, setSessions] = useState<ApiSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  useEffect(() => {
    if (!opened) return;
    let cancelled = false;
    setLoading(true); setError("");
    fetchSessions(token, { limit: 100, offset: 0 }).then((items) => { if (!cancelled) { setSessions(items); setHasMore(items.length === 100); } })
      .catch((cause) => { if (!cancelled) setError(friendlyMessage(cause)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [opened, token, revision]);
  async function loadMore() {
    setLoading(true); setError("");
    try {
      const items = await fetchSessions(token, { limit: 100, offset: sessions.length });
      setSessions((current) => [...new Map([...current, ...items].map((session) => [session.id, session])).values()]);
      setHasMore(items.length === 100);
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setLoading(false); }
  }
  const selected = context.session_ids.split(",").map((id) => id.trim()).filter(Boolean);
  const unknown = selected.filter((id) => !sessions.some((session) => session.id === id));
  const active = fields.filter(([key]) => context[key]).length + Number(selected.length > 0);
  return <details className="border-b border-border-soft pb-4" onToggle={(event) => { if (event.currentTarget.open) setOpened(true); }}>
    <summary className="cursor-pointer text-sm font-medium">Cohort details{active ? ` · ${active} active` : ""}</summary>
    <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {fields.map(([key, label, placeholder, maxLength]) => <label key={key} className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground">{label}<Input value={context[key]} placeholder={placeholder} maxLength={maxLength} onChange={(event) => onChange({ [key]: event.target.value })} /></label>)}
      <label className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground sm:col-span-2 xl:col-span-3">Specific sessions
        <select multiple value={selected} size={Math.max(2, Math.min(5, sessions.length + unknown.length))} className="w-full rounded-md border border-input bg-background p-2 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-ring" onChange={(event) => onChange({ session_ids: Array.from(event.currentTarget.selectedOptions, (option) => option.value).join(",") })}>
          {sessions.map((session) => <option key={session.id} value={session.id}>{session.title}{session.recorded_at ? ` · ${session.recorded_at.slice(0, 10)}` : ""}{session.session_kind ? ` · ${session.session_kind}` : ""}</option>)}
          {unknown.map((id) => <option key={id} value={id}>Selected session · {id}</option>)}
        </select>
      </label>
    </div>
    <p className="mt-2 text-xs text-muted-foreground">Select multiple sessions with Ctrl or Command. An empty selection includes every session that matches the filters. Drill and contact values match the film's recorded context.</p>
    {loading ? <p role="status" className="mt-2 text-xs text-muted-foreground">Loading sessions…</p> : error ? <p role="alert" className="mt-2 text-xs text-status-danger">{error}<Button size="sm" variant="link" onClick={() => setRevision((value) => value + 1)}>Retry sessions</Button></p> : !sessions.length ? <p className="mt-2 text-xs text-muted-foreground">No named sessions are available.</p> : null}
    {hasMore ? <Button size="sm" variant="outline" className="mt-2" disabled={loading} onClick={loadMore}>Load older sessions</Button> : null}
    {active > 0 ? <Button size="sm" variant="ghost" className="mt-2" onClick={() => onChange({ session_ids: "", personnel: "", formation: "", situation: "", drill_type: "", contact_level: "", opponent: "" })}>Clear cohort details</Button> : null}
  </details>;
}
