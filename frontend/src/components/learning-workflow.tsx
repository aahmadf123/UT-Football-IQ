"use client";

import { useCallback, useEffect, useState } from "react";
import { useAppState } from "@/lib/app-state";
import { useWorkspaceContext } from "@/lib/workspace-context";
import { createFeatureManifest, evaluateLearningCandidate, fetchSessions, listFeatureManifests, listLearningCandidates, previewFeatureManifest, queueCoverageTraining, type LearningFeatureManifest, type LearningFeaturePreview, type LearningSessionSplit } from "@/lib/api";
import type { ApiSession, ModelVersion } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { StatusBadge, toneForJobStatus } from "@/components/composite/status-badge";
import { friendlyMessage, processingErrorMessage } from "@/lib/api-error";
import { ExpectedOutcomesTraining } from "@/components/expected-outcomes-training";

type Split = LearningSessionSplit["split"];
type Evaluation = { passed: boolean; accuracy: number; macro_f1: number; sample_count: number };
const SPLITS: Array<{ value: Split; label: string }> = [{ value: "train", label: "Training" }, { value: "calibration", label: "Calibration" }, { value: "holdout", label: "Holdout" }];
const COVERAGE_NAMES: Record<string, string> = { cover_0: "Cover 0", cover_1: "Cover 1", cover_2_mof: "Cover 2 MOF", cover_2_shell: "Cover 2 shell", cover_3: "Cover 3", cover_4: "Cover 4", man_free: "Man free", bracket_match: "Bracket / match", cover_6: "Cover 6" };

export function LearningWorkflow({ onUpdated }: { onUpdated: () => Promise<void> | void }) {
  const { authToken, data } = useAppState();
  const workspace = useWorkspaceContext();
  const [sessions, setSessions] = useState<ApiSession[]>([]);
  const [history, setHistory] = useState<LearningFeatureManifest[]>([]);
  const [selectedSplits, setSelectedSplits] = useState<Record<string, Split | "">>({});
  const [name, setName] = useState("Coverage learning set");
  const [preview, setPreview] = useState<LearningFeaturePreview | null>(null);
  const [manifest, setManifest] = useState<LearningFeatureManifest | null>(null);
  const [candidates, setCandidates] = useState<ModelVersion[]>([]);
  const [trainingId, setTrainingId] = useState<string | null>(null);
  const [requestedTrainingStatus, setRequestedTrainingStatus] = useState("queued");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [evaluations, setEvaluations] = useState<Record<string, Evaluation>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try { const [film, saved] = await Promise.all([fetchSessions(authToken), listFeatureManifests(authToken)]); setSessions(film); setHistory(saved); setError(null); }
    catch (cause) { setError(friendlyMessage(cause)); }
    finally { setLoading(false); }
  }, [authToken]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!manifest) { setCandidates([]); return; }
    let cancelled = false;
    const refresh = async () => {
      try { const models = await listLearningCandidates(manifest.id, authToken); if (!cancelled) setCandidates(models); }
      catch (cause) { if (!cancelled) setError(friendlyMessage(cause)); }
    };
    void refresh();
    const interval = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [manifest, authToken]);

  const selected: LearningSessionSplit[] = Object.entries(selectedSplits).flatMap(([session_id, split]) => split ? [{ session_id, split }] : []);
  const splitsComplete = SPLITS.every((split) => selected.some((item) => item.split === split.value));
  const trainingJob = data.jobs.find((job) => job.id === trainingId) ?? data.jobs.find((job) => job.job_type === "train" && manifest && job.input_artifacts?.training_dataset_id === manifest.training_dataset_id);
  const visibleTrainingId = trainingId ?? trainingJob?.id;
  const trainingStatus = trainingJob?.status ?? (trainingId ? requestedTrainingStatus : undefined);

  function selectSaved(id: string) {
    const saved = history.find((item) => item.id === id);
    setManifest(saved ?? null); setPreview(null); setTrainingId(null); setNotice(null);
    setName(saved?.name ?? "Coverage learning set");
    setSelectedSplits(saved?.sessions ? Object.fromEntries(saved.sessions.map((item) => [item.session_id, item.split])) : {});
  }

  function changeSplit(id: string, value: Split | "") {
    setSelectedSplits((current) => ({ ...current, [id]: value }));
    setPreview(null); setManifest(null); setTrainingId(null); setNotice(null);
  }

  async function inspect() {
    setBusy("preview"); setError(null); setNotice(null);
    try { setPreview(await previewFeatureManifest({ name: name.trim(), sessions: selected }, authToken)); }
    catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(null); }
  }

  async function build() {
    if (!preview?.ready) return;
    setBusy("build"); setError(null);
    try {
      const created = await createFeatureManifest({ name: name.trim(), sessions: selected }, authToken);
      setManifest(created); setNotice("Feature set saved with separate training, calibration, and holdout footage.");
      await load(); await onUpdated();
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(null); }
  }

  async function train() {
    if (!manifest) return;
    setBusy("train"); setError(null); setNotice(null);
    try {
      const job = await queueCoverageTraining({ training_dataset_id: manifest.training_dataset_id, features_uri: manifest.features_uri }, authToken);
      setTrainingId(job.job_id); setRequestedTrainingStatus(job.status); setNotice(`CPU coverage training job is ${job.status}. Progress stays in the connected library.`);
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(null); }
  }

  async function evaluate(model: ModelVersion) {
    if (!manifest) return;
    setBusy(model.id); setError(null);
    try {
      const result = await evaluateLearningCandidate(model.id, manifest.benchmark_id, authToken);
      setEvaluations((current) => ({ ...current, [model.id]: result }));
      setCandidates(await listLearningCandidates(manifest.id, authToken));
      await onUpdated();
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(null); }
  }

  return <section className="rounded-xl border border-border-soft p-4" aria-label={workspace.local ? "Local coverage learning workflow" : "Team coverage learning workflow"}>
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-base font-semibold">Train and evaluate a {workspace.local ? "local" : "team"} coverage model</h3><p className="mt-2 max-w-3xl text-sm leading-relaxed text-muted-foreground">Choose separate sessions for training, confidence calibration, and a final holdout test. Only detailed processing results with reviewed human coverage labels can enter the feature set.</p></div><Button size="sm" variant="outline" onClick={() => void load()} disabled={loading}>Refresh film</Button></div>
    {loading ? <p role="status" className="mt-3 text-sm">Loading available sessions and feature sets…</p> : null}
    {error ? <p role="alert" className="mt-3 text-sm text-status-danger">{error}</p> : null}
    {notice ? <p role="status" className="mt-3 rounded-lg border border-status-info/40 p-3 text-sm">{notice}</p> : null}
    {history.length ? <div className="mt-4"><Label htmlFor="saved-learning-set">Saved feature set</Label><NativeSelect id="saved-learning-set" className="mt-2" value={manifest?.id ?? ""} onChange={(event) => selectSaved(event.target.value)}><option value="">Build a new feature set</option>{history.map((item) => <option key={item.id} value={item.id}>{item.name || item.id.slice(0, 8)} · {Object.values(item.counts).reduce((sum, count) => sum + count, 0)} examples</option>)}</NativeSelect></div> : null}
    <details className="mt-4 rounded-lg border border-border-soft p-4" open={!manifest || undefined}><summary className="cursor-pointer text-sm font-medium">1. Choose footage and preview readiness</summary><div className="mt-4 flex flex-col gap-4"><div><Label htmlFor="learning-feature-name">Feature set name</Label><Input id="learning-feature-name" value={name} maxLength={120} onChange={(event) => { setName(event.target.value); setPreview(null); }} className="mt-2" /></div>
      {sessions.length ? <div className="max-h-80 overflow-y-auto rounded-lg border border-border-soft">{sessions.map((session) => <div key={session.id} className="flex flex-wrap items-center justify-between gap-3 border-b border-border-soft p-3 last:border-0"><div className="min-w-0"><p className="font-medium">{session.title}</p><p className="mt-1 text-xs text-muted-foreground">{session.recorded_at?.slice(0, 10) || "Date not specified"} · {session.recordings.length} recordings · {session.library_role === "opponent" ? "Opponent film" : "Toledo film"}</p></div><NativeSelect aria-label={`Learning split for ${session.title}`} value={selectedSplits[session.id] ?? ""} onChange={(event) => changeSplit(session.id, event.target.value as Split | "")} disabled={busy !== null}><option value="">Do not include</option>{SPLITS.map((split) => <option key={split.value} value={split.value}>{split.label}</option>)}</NativeSelect></div>)}</div> : <p className="text-sm text-muted-foreground">Add sessions in Film Room, run detailed processing, and review coverage labels to begin.</p>}
      <p className="text-sm text-muted-foreground">Assign at least one separate session to each group: training, calibration, and holdout.</p>
      <Button variant="outline" className="self-start" disabled={!splitsComplete || !name.trim() || busy !== null} onClick={() => void inspect()}>{busy === "preview" ? "Checking footage…" : "Preview training readiness"}</Button>
      {preview ? <div className="rounded-lg bg-secondary/30 p-4"><div className="grid gap-3 sm:grid-cols-3">{SPLITS.map((split) => <div key={split.value}><p className="text-sm font-medium">{split.label}</p><p className="mt-1 text-sm text-muted-foreground">{preview.counts[split.value] ?? 0} eligible examples · {preview.required[split.value] ?? 0} required</p></div>)}</div>{preview.issues.length ? <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-status-warn">{preview.issues.map((issue) => <li key={issue}>{issue}</li>)}</ul> : <p className="mt-3 text-sm text-status-ok">{preview.ready ? "This selection meets the data requirements." : "More eligible film is needed before training."}</p>}<details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer">Coverage examples per class</summary><CoverageClassCounts counts={preview.class_counts} /></details><Button className="mt-4" disabled={!preview.ready || busy !== null} onClick={() => void build()}>{busy === "build" ? "Saving feature set…" : "Build feature set"}</Button></div> : null}
    </div></details>
    {manifest ? <div className="mt-4 rounded-lg border border-border-soft p-4"><h4 className="font-medium">2. Train with the connected processor</h4><p className="mt-2 text-sm text-muted-foreground">The saved feature set contains {manifest.counts.train ?? 0} training, {manifest.counts.calibration ?? 0} calibration, and {manifest.counts.holdout ?? 0} holdout examples.</p><Button className="mt-3" disabled={busy !== null || trainingStatus === "queued" || trainingStatus === "running" || trainingStatus === "paused"} onClick={() => void train()}>{busy === "train" ? "Queueing…" : "Train CPU coverage model"}</Button>{visibleTrainingId ? <div className="mt-3 flex flex-wrap items-center gap-3 text-sm"><StatusBadge dot tone={toneForJobStatus(trainingStatus ?? "queued")}>{trainingStatus ?? "queued"}</StatusBadge><span className="text-muted-foreground">{trainingJob?.error_message ? processingErrorMessage(trainingJob.error_stage, trainingJob.error_message) : "Progress is saved in the connected job queue."}</span></div> : null}
      <h4 className="mt-5 font-medium">3. Evaluate the candidate on held-out film</h4>{candidates.length ? <div className="mt-3 space-y-3">{candidates.map((candidate) => {
        const measured = evaluations[candidate.id] ?? persistedEvaluation(candidate);
        const predictionCount = candidate.prediction_count ?? (typeof candidate.metrics?.prediction_count === "number" ? candidate.metrics.prediction_count : 0);
        return <div key={candidate.id} className="rounded-lg border border-border-soft p-3"><div className="flex flex-wrap items-center justify-between gap-3"><div><p className="font-medium">{candidate.model_name} · {candidate.version}</p><p className="mt-1 text-xs text-muted-foreground">{predictionCount} persisted model predictions{candidate.required_predictions ? ` · ${candidate.required_predictions} required` : ""}</p></div><Button size="sm" variant="outline" disabled={busy !== null || candidate.ready_for_evaluation !== true} onClick={() => void evaluate(candidate)}>{busy === candidate.id ? "Evaluating…" : "Evaluate held-out film"}</Button></div>{measured ? <p className="mt-3 text-sm">Accuracy {(measured.accuracy * 100).toFixed(1)}% · Macro F1 {(measured.macro_f1 * 100).toFixed(1)}% · {measured.sample_count} examples · <strong className={measured.passed ? "text-status-ok" : "text-status-warn"}>{measured.passed ? "Release thresholds passed" : "Release thresholds not met"}</strong></p> : null}</div>;
      })}</div> : <p className="mt-2 text-sm text-muted-foreground">A candidate appears here after successful training. It stays experimental until the holdout evaluation passes.</p>}<p className="mt-4 text-sm text-muted-foreground">Validated versions can be promoted or restored in Registered model versions below.</p>
    </div> : null}
    <ExpectedOutcomesTraining onUpdated={onUpdated} />
  </section>;
}

function persistedEvaluation(model: ModelVersion): Evaluation | null {
  const value = model.metrics?.benchmark;
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  if (typeof record.passed !== "boolean" || typeof record.accuracy !== "number" || typeof record.macro_f1 !== "number" || typeof record.sample_count !== "number") return null;
  return { passed: record.passed, accuracy: record.accuracy, macro_f1: record.macro_f1, sample_count: record.sample_count };
}

function CoverageClassCounts({ counts }: { counts: LearningFeaturePreview["class_counts"] }) {
  const groups: Record<Split, Record<string, number>> = { train: {}, calibration: {}, holdout: {} };
  for (const split of SPLITS) {
    const values = counts[split.value];
    if (values && typeof values === "object" && !Array.isArray(values)) {
      for (const [name, count] of Object.entries(values)) {
        if (typeof count === "number" && Number.isFinite(count) && count >= 0) groups[split.value][name] = count;
      }
    }
  }
  const observed = new Set(Object.values(groups).flatMap((group) => Object.keys(group)));
  const classes = [...Object.keys(COVERAGE_NAMES).filter((name) => observed.has(name)), ...[...observed].filter((name) => !(name in COVERAGE_NAMES)).sort()];
  if (!classes.length) return <p className="mt-2 text-sm">No reviewed coverage examples are eligible in these sessions.</p>;
  return <div className="mt-3 overflow-x-auto"><table className="w-full min-w-96 text-left text-sm"><caption className="sr-only">Reviewed coverage examples by training group</caption><thead><tr><th scope="col" className="px-2 py-2 font-medium">Coverage</th>{SPLITS.map((split) => <th key={split.value} scope="col" className="px-2 py-2 text-right font-medium">{split.label}</th>)}</tr></thead><tbody>{classes.map((name) => <tr key={name} className="border-t border-border-soft"><th scope="row" className="px-2 py-2 font-medium">{COVERAGE_NAMES[name] ?? name.replaceAll("_", " ")}</th>{SPLITS.map((split) => <td key={split.value} className="px-2 py-2 text-right tabular-nums">{groups[split.value][name] ?? 0}</td>)}</tr>)}</tbody></table></div>;
}
