"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, Cpu, RefreshCw, Wrench } from "lucide-react";
import { useAppState } from "@/lib/app-state";
import { fetchSystemStatus, repairSystem } from "@/lib/api";
import { apiBase, configureApiUrl } from "@/lib/endpoints";
import type { SystemStatus } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useWorkspaceContext } from "@/lib/workspace-context";

const REPAIRS = [
  { action: "restart-worker", label: "Restart processing" },
  { action: "install-detector", label: "Install detection model" },
  { action: "clear-cache", label: "Clear temporary cache" },
  { action: "restart-api", label: "Restart library service" },
] as const;

export function LocalSystemPanel() {
  const { authToken, currentRole, refreshData } = useAppState();
  const workspace = useWorkspaceContext();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [origin, setOrigin] = useState("");
  const canRepair = currentRole === "admin";

  const load = useCallback(async () => {
    try { setStatus(await fetchSystemStatus(authToken)); setError(null); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setLoading(false); }
  }, [authToken]);

  useEffect(() => { setOrigin(apiBase()); void load(); const interval = setInterval(load, 10000); return () => clearInterval(interval); }, [load]);

  async function repair(action: typeof REPAIRS[number]["action"]) {
    setPending(action); setNotice(null); setError(null);
    try {
      await repairSystem(action, authToken);
      setNotice("The local launcher has queued this action. Status updates automatically as services reconnect.");
      await load();
      await refreshData();
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setPending(null); }
  }

  function switchLibrary(event: React.FormEvent) {
    event.preventDefault();
    try { configureApiUrl(origin.trim()); window.location.assign("/login/"); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }

  return <Card>
    <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-base font-semibold"><Cpu className="size-5" /> {workspace.local ? "Local processing" : "Team processing"}</h2><p className="mt-1 text-sm text-muted-foreground">Check the connected library, processing profiles, and models.</p></div><Button variant="outline" size="sm" disabled={loading} onClick={() => { setLoading(true); void load(); }}><RefreshCw className="size-4" /> Check again</Button></CardHeader>
    <CardContent className="flex flex-col gap-4">
      {loading && !status ? <p role="status">Checking connected services…</p> : null}
      {error ? <div role="alert" className="rounded-lg border border-status-danger/40 bg-status-danger/5 p-4"><p className="font-medium">Could not check the connected library</p><p className="mt-1 text-sm text-muted-foreground">{error}</p><p className="mt-2 text-sm">{workspace.local ? "Open the Football IQ launcher and use Check again after the library starts." : "Check the team API connection, then try again."}</p></div> : null}
      {notice ? <p role="status" className="rounded-lg border border-status-info/40 p-3 text-sm">{notice}</p> : null}
      {error && status ? <p className="text-sm text-muted-foreground">The status below is from the last successful check.</p> : null}
      {status ? <>
        <div className="grid gap-3 sm:grid-cols-3">
          <HealthItem title="Library" ready={status.storage.writable} detail={status.storage.writable ? "Storage is ready" : "Storage needs attention"} />
          <HealthItem title="Processing" ready={status.capabilities.processing} detail={status.capabilities.processing ? "A processor is connected. Available profiles are listed below." : status.worker.reason || "Processing worker is not connected"} />
          <HealthItem title="Film review" ready={status.capabilities.film_review} detail={status.capabilities.film_review ? "Playback and annotations available" : "Start the library service"} />
        </div>
        <div className="grid gap-3 sm:grid-cols-3">{status.profiles.map((profile) => <div key={profile.id} className="rounded-lg border border-border-soft p-4"><div className="flex items-center justify-between gap-2"><h3 className="font-medium">{profile.label}</h3><span className={profile.available ? "text-xs text-status-ok" : "text-xs text-muted-foreground"}>{profile.available ? "Available" : "Unavailable"}</span></div><p className="mt-2 text-sm leading-relaxed text-muted-foreground">{profile.description}</p></div>)}</div>
        {status.storage.free_bytes !== null ? <p className="text-sm text-muted-foreground">{(status.storage.free_bytes / 1024 ** 3).toFixed(1)} GB of free library storage.</p> : null}
        {status.limitations.length ? <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">{status.limitations.map((item) => <li key={item}>{item}</li>)}</ul> : null}
        <details className="rounded-lg border border-border-soft p-4"><summary className="cursor-pointer text-sm font-medium">Models and connected processors</summary><div className="mt-3 space-y-3">{status.worker.workers.length ? status.worker.workers.map((worker) => <div key={worker.worker_id} className="rounded-lg bg-secondary/40 p-3"><p className="text-sm font-medium">{worker.worker_id} · {worker.device} · {worker.online ? "Connected" : "Not connected"}</p><p className="mt-1 text-xs text-muted-foreground">Profiles: {worker.profiles.join(", ") || "None reported"}</p><pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs text-muted-foreground">{JSON.stringify(worker.models, null, 2)}</pre></div>) : <p className="text-sm text-muted-foreground">No processor has reported its capabilities yet.</p>}</div></details>
        {canRepair ? <div className="rounded-lg border border-border-soft p-4"><h3 className="flex items-center gap-2 font-medium"><Wrench className="size-4" /> Recovery actions</h3>{status.supervisor ? <div className="mt-3 flex flex-wrap gap-2">{REPAIRS.map(({action, label}) => <Button key={action} variant="outline" size="sm" disabled={pending !== null} onClick={() => void repair(action)}>{pending === action ? "Requesting…" : label}</Button>)}</div> : <p className="mt-2 text-sm text-muted-foreground">Start this library with the local launcher to enable service recovery.</p>}</div> : null}
      </> : null}
      <details className="rounded-lg border border-border-soft p-4"><summary className="cursor-pointer text-sm font-medium">Connect to another library</summary><form onSubmit={switchLibrary} className="mt-3 flex flex-col gap-3"><p className="text-sm text-muted-foreground">Each local installation keeps its own film. Changing libraries signs you out so accounts and filters stay with the correct library.</p><Label htmlFor="library-origin">Library address</Label><Input id="library-origin" value={origin} onChange={(event) => setOrigin(event.target.value)} placeholder="Leave blank for this computer" /><Button className="self-start" variant="outline">Connect</Button></form></details>
    </CardContent>
  </Card>;
}

function HealthItem({ title, ready, detail }: { title: string; ready: boolean; detail: string }) {
  const Icon = ready ? CheckCircle2 : CircleAlert;
  return <div className="rounded-lg border border-border-soft p-4"><h3 className="flex items-center gap-2 font-medium"><Icon className={ready ? "size-4 text-status-ok" : "size-4 text-status-warn"} />{title}</h3><p className="mt-2 text-sm text-muted-foreground">{detail}</p></div>;
}
