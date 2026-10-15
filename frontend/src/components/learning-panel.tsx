"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, RefreshCw } from "lucide-react";
import { useAppState } from "@/lib/app-state";
import { exportCorrections, getTrainingDatasetDownloadUrl, listLearningBenchmarks, listModelVersions, listTrainingDatasets, promoteModelVersion } from "@/lib/api";
import type { LearningBenchmark, ModelVersion, TrainingDataset } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LearningWorkflow } from "@/components/learning-workflow";

export function LearningPanel() {
  const { authToken, currentRole } = useAppState();
  const [datasets, setDatasets] = useState<TrainingDataset[]>([]);
  const [models, setModels] = useState<ModelVersion[]>([]);
  const [benchmarks, setBenchmarks] = useState<LearningBenchmark[]>([]);
  const [scope, setScope] = useState("general");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const permitted = currentRole === "admin" || currentRole === "analyst";

  const load = useCallback(async () => {
    if (!permitted) return;
    setLoading(true);
    try {
      const [snapshots, versions, holdouts] = await Promise.all([listTrainingDatasets(authToken), listModelVersions(authToken), listLearningBenchmarks(authToken)]);
      setDatasets(snapshots); setModels(versions); setBenchmarks(holdouts); setError(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setLoading(false); }
  }, [authToken, permitted]);

  useEffect(() => { void load(); }, [load]);

  async function materialize() {
    setBusy("export"); setNotice(null); setError(null);
    try {
      const result = await exportCorrections(scope.trim() || "general", authToken);
      await load();
      setNotice(result.training_dataset_id ? `Created a saved snapshot with ${result.exported_count} human labels. It is available below.` : "No new eligible corrections to export. Save corrections while reviewing film, then try again.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  }

  async function download(snapshot: TrainingDataset) {
    setBusy(snapshot.id); setError(null);
    try {
      const { download_url } = await getTrainingDatasetDownloadUrl(snapshot.id, authToken);
      const link = document.createElement("a"); link.href = download_url; link.download = `learning-snapshot-${snapshot.id}.jsonl`; link.target = "_blank"; link.rel = "noopener noreferrer"; document.body.appendChild(link); link.click(); link.remove();
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  }

  async function promote(model: ModelVersion) {
    setBusy(model.id); setNotice(null); setError(null);
    try { await promoteModelVersion(model.id, "production", authToken); await load(); setNotice(`${model.model_name} ${model.version} is now the registered production version.`); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(null); }
  }

  if (!permitted) return null;

  return <Card><CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3"><div><h2 className="text-base font-semibold">Learning from coach corrections</h2><p className="mt-1 max-w-3xl text-sm leading-relaxed text-muted-foreground">Corrections become inspectable human labels. A model version needs measured results on separate holdout film before promotion.</p></div><Button variant="outline" size="sm" onClick={() => void load()} disabled={loading}><RefreshCw className="size-4" /> Refresh</Button></CardHeader><CardContent className="flex flex-col gap-4">
    {loading ? <p role="status" className="text-sm">Loading learning history…</p> : null}
    {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
    {notice ? <p role="status" className="rounded-lg border border-status-info/40 p-3 text-sm">{notice}</p> : null}
    <LearningWorkflow onUpdated={load} />
    <form onSubmit={(event) => { event.preventDefault(); void materialize(); }} className="flex flex-wrap items-end gap-3 rounded-lg border border-border-soft p-4"><div><Label htmlFor="learning-scope">Snapshot name / model scope</Label><Input id="learning-scope" value={scope} onChange={(event) => setScope(event.target.value)} maxLength={100} className="mt-2" /></div><Button disabled={busy !== null || loading}>{busy === "export" ? "Creating…" : "Create learning snapshot"}</Button><p className="basis-full text-sm text-muted-foreground">Snapshots contain new eligible corrections and their source labels. Creating a snapshot does not change model weights.</p></form>
    <div><h3 className="mb-3 font-medium">Human label snapshots</h3>{datasets.length ? <div className="space-y-3">{datasets.map((snapshot) => <div key={snapshot.id} className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-border-soft p-4"><div><p className="font-medium">{snapshot.model_scope} · {snapshot.row_count} labels</p><p className="mt-1 text-sm text-muted-foreground">{snapshot.snapshot_date} · {snapshot.source_correction_ids?.length ?? 0} source corrections</p><details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">View snapshot lineage</summary><pre className="mt-2 max-w-2xl overflow-x-auto whitespace-pre-wrap">{JSON.stringify({ dataset: snapshot.id, source_labels: snapshot.source_label_ids, source_corrections: snapshot.source_correction_ids, change: snapshot.changelog }, null, 2)}</pre></details></div><Button size="sm" variant="outline" disabled={!snapshot.artifact_uri || busy !== null} onClick={() => void download(snapshot)}><Download className="size-4" /> Download labels</Button></div>)}</div> : <p className="text-sm text-muted-foreground">No learning snapshots yet. Review film and save a correction to begin.</p>}</div>
    <div><h3 className="mb-3 font-medium">Registered model versions</h3>{models.length ? <div className="space-y-3">{models.map((model) => {
      const benchmark = model.metrics?.benchmark;
      const passed = Boolean(benchmark && typeof benchmark === "object" && "passed" in benchmark && benchmark.passed === true && model.metrics?.evaluation_id && model.artifact_uri);
      return <div key={model.id} className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-border-soft p-4"><div><p className="font-medium">{model.model_name} · {model.version}</p><p className="mt-1 text-sm text-muted-foreground">{model.promoted_stage} · {model.model_type} · {passed ? "Passing human holdout evaluation recorded" : "Passing holdout evaluation required"}</p></div>{currentRole === "admin" && model.promoted_stage !== "production" ? <Button variant="outline" size="sm" disabled={!passed || busy !== null} onClick={() => void promote(model)}>{model.promoted_stage === "retired" ? "Restore validated version" : "Promote validated version"}</Button> : null}</div>;
    })}</div> : <p className="text-sm text-muted-foreground">No trained model versions registered yet.</p>}</div>
    <div><h3 className="mb-2 font-medium">Holdout benchmarks</h3>{benchmarks.length ? <ul className="space-y-2 text-sm text-muted-foreground">{benchmarks.map((benchmark) => <li key={benchmark.id}>{benchmark.name} · {benchmark.samples?.length ?? 0} human-labeled examples</li>)}</ul> : <p className="text-sm text-muted-foreground">No separate evaluation benchmark is registered yet. Training film must be kept separate from holdout film.</p>}</div>
  </CardContent></Card>;
}
