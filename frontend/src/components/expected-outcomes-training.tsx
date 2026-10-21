"use client";

import { useState } from "react";
import { useAppState } from "@/lib/app-state";
import { evaluateExpectedOutcomesCandidate, queueExpectedOutcomesTraining, type ExpectedOutcomesTrainingRequest } from "@/lib/api";
import { friendlyMessage, processingErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { StatusBadge, toneForJobStatus } from "@/components/composite/status-badge";
import { ExpectedFeatureExports } from "@/components/expected-feature-exports";

const TASKS = { completion: "Completion probability", rushing_gain: "Rushing gain", yac: "Yards after catch", pressure: "Contextual pressure" };
type Task = NonNullable<ExpectedOutcomesTrainingRequest["task"]>;
function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function measured(value: unknown): string { return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "Unavailable"; }

export function ExpectedOutcomesTraining({ onUpdated }: { onUpdated: () => Promise<void> | void }) {
  const { authToken, data } = useAppState();
  const [manifest, setManifest] = useState("");
  const [checksum, setChecksum] = useState("");
  const [output, setOutput] = useState("");
  const [task, setTask] = useState<Task | "">("");
  const [queuedId, setQueuedId] = useState<string | null>(null);
  const [queuedStatus, setQueuedStatus] = useState("queued");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [evaluating, setEvaluating] = useState<string | null>(null);
  const [evaluations, setEvaluations] = useState<Record<string, boolean>>({});
  const [verifications, setVerifications] = useState<Record<string, { jobId: string; status: string }>>({});
  const [evaluationReasons, setEvaluationReasons] = useState<Record<string, string[]>>({});
  const matching = data.jobs.find((job) => job.job_type === "train" && job.input_artifacts?.training_variant === "expected_outcomes"
    && job.input_artifacts.manifest_sha256 === checksum.trim() && job.input_artifacts.manifest_path === manifest.trim()
    && job.input_artifacts.output_dir === output.trim() && (job.input_artifacts.task ?? "") === task);
  const selectedJob = queuedId ? data.jobs.find((item) => item.id === queuedId) : matching;
  const recentJob = queuedId || matching ? undefined : data.jobs.find((item) => item.job_type === "train" && item.input_artifacts?.training_variant === "expected_outcomes");
  const job = selectedJob ?? recentJob;
  const status = queuedId ? (selectedJob?.status ?? queuedStatus) : job?.status;
  const active = ((queuedId !== null || matching !== undefined) && ["queued", "running", "paused"].includes(status ?? ""));
  const ready = manifest.trim().length > 0 && /^[0-9a-f]{64}$/.test(checksum.trim()) && output.trim().length > 0;
  const reports = Object.entries(record(job?.output_artifacts?.models));
  const registered = record(job?.output_artifacts?.registered);

  async function verify(modelId: string) {
    setEvaluating(modelId); setError(null);
    try {
      const result = await evaluateExpectedOutcomesCandidate(modelId, authToken);
      if ("passed" in result) {
        setEvaluations((current) => ({ ...current, [modelId]: result.passed }));
        setEvaluationReasons((current) => ({ ...current, [modelId]: result.missing_data_reasons ?? [] }));
      } else {
        setVerifications((current) => ({ ...current, [modelId]: { jobId: result.job_id, status: result.status } }));
      }
      await onUpdated();
    }
    catch (cause) { setError(friendlyMessage(cause)); }
    finally { setEvaluating(null); }
  }

  async function queue() {
    if (!ready || active || busy) return;
    setBusy(true); setError(null);
    try {
      const result = await queueExpectedOutcomesTraining({ manifest_path: manifest.trim(), manifest_sha256: checksum.trim(), output_dir: output.trim(), ...(task ? { task } : {}) }, authToken);
      setQueuedId(result.job_id); setQueuedStatus(result.status);
      await onUpdated();
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setBusy(false); }
  }

  return <details className="mt-5 rounded-lg border border-border-soft p-4">
    <summary className="cursor-pointer text-sm font-medium">Train expected-outcome candidates</summary>
    <div className="mt-4 space-y-4" role="group" aria-label="Expected-outcome training">
      <ExpectedFeatureExports />
      <p className="max-w-3xl text-sm text-muted-foreground">Use a prepared reviewed dataset on the connected processor. It must include the original film, prediction-time features, human outcomes, and separate training, calibration, and holdout sessions. Training also evaluates the complete holdout against the training-population baseline.</p>
      <div className="grid gap-4 md:grid-cols-2">
        <div><Label htmlFor="expected-manifest">Reviewed manifest file</Label><Input id="expected-manifest" value={manifest} maxLength={2048} onChange={(event) => { setManifest(event.target.value); setQueuedId(null); }} className="mt-2" /></div>
        <div><Label htmlFor="expected-checksum">Manifest SHA-256</Label><Input id="expected-checksum" value={checksum} maxLength={64} onChange={(event) => { setChecksum(event.target.value); setQueuedId(null); }} className="mt-2 font-mono" /></div>
        <div><Label htmlFor="expected-output">New candidate folder</Label><Input id="expected-output" value={output} maxLength={2048} onChange={(event) => { setOutput(event.target.value); setQueuedId(null); }} className="mt-2" /><p className="mt-1 text-xs text-muted-foreground">Choose a folder that does not exist yet. Evidence and existing candidates are preserved.</p></div>
        <div><Label htmlFor="expected-task">Expected outcome</Label><NativeSelect id="expected-task" value={task} onChange={(event) => { setTask(event.target.value as Task | ""); setQueuedId(null); }} className="mt-2"><option value="">All available outcomes</option>{Object.entries(TASKS).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</NativeSelect></div>
      </div>
      <Button disabled={!ready || active || busy} onClick={() => void queue()}>{busy ? "Queueing…" : "Queue expected-outcome training"}</Button>
      {error ? <p role="alert" className="text-sm text-status-danger">{error}</p> : null}
      {job ? <p className="text-xs text-muted-foreground">{recentJob ? "Recent training request" : "Selected training request"}: <span className="font-mono">{job.id}</span> · Manifest <span className="break-all font-mono">{typeof job.input_artifacts?.manifest_sha256 === "string" ? job.input_artifacts.manifest_sha256 : "checksum unavailable"}</span>. Results below belong to this request.</p> : null}
      {status ? <div role="status" className="flex flex-wrap items-center gap-3 text-sm"><StatusBadge dot tone={toneForJobStatus(status)}>{status}</StatusBadge><span>{job?.error_message ? processingErrorMessage(job.error_stage, job.error_message) : "Progress is saved in the connected job queue."}</span></div> : null}
      {reports.length ? <div className="overflow-x-auto"><table className="w-full text-left text-sm"><caption className="mb-2 text-left font-medium">Expected-outcome holdout evaluation</caption><thead><tr><th scope="col" className="p-2">Outcome</th><th scope="col" className="p-2">Plays</th><th scope="col" className="p-2">Brier / baseline</th><th scope="col" className="p-2">Log loss / baseline</th><th scope="col" className="p-2">MAE / baseline (yd)</th><th scope="col" className="p-2">RMSE / baseline (yd)</th></tr></thead><tbody>{reports.map(([name, value]) => {
        const report = record(record(value).report), metrics = record(report.metrics), baseline = record(record(report.baseline).metrics);
        return <tr key={name} className="border-t border-border-soft"><th scope="row" className="p-2">{TASKS[name as Task] ?? name}</th><td className="p-2">{typeof report.count === "number" ? report.count : "Unavailable"}</td>{["brier", "log_loss", "mae", "rmse"].map((metric) => <td key={metric} className="p-2 tabular-nums">{metrics[metric] === undefined ? "—" : `${measured(metrics[metric])} / ${measured(baseline[metric])}`}</td>)}</tr>;
      })}</tbody></table></div> : null}
      {Object.entries(registered).map(([name, value]) => {
        const modelId = record(value).model_version_id;
        if (typeof modelId !== "string") return null;
        const verification = verifications[modelId];
        const verificationJob = data.jobs.find((item) => item.id === verification?.jobId);
        const verificationStatus = verificationJob?.status ?? verification?.status;
        const pending = ["queued", "running", "paused"].includes(verificationStatus ?? "");
        return <div key={modelId} className="rounded-lg border border-border-soft p-3 text-sm"><div className="flex flex-wrap items-center gap-3"><span>{TASKS[name as Task] ?? name}</span><Button size="sm" variant="outline" disabled={evaluating !== null || pending} onClick={() => void verify(modelId)}>{evaluating === modelId ? "Verifying…" : verificationStatus === "succeeded" ? "Record verified holdout evaluation" : `Verify ${TASKS[name as Task] ?? name} holdout`}</Button>{verificationStatus ? <span role="status">Independent verification: {verificationStatus}</span> : null}{evaluations[modelId] !== undefined ? <span>{evaluations[modelId] ? "Holdout gate passed; admin model promotion is required." : "Holdout gate did not pass; candidate remains experimental."}</span> : null}</div>{verificationJob?.error_message ? <p className="mt-2 text-status-danger">{processingErrorMessage(verificationJob.error_stage, verificationJob.error_message)}</p> : null}{evaluationReasons[modelId]?.length ? <p className="mt-2 text-muted-foreground">Release evidence still needs the balanced 240-play, 12-source benchmark and its reviewed source and label attestations.</p> : null}</div>;
      })}
      <p className="text-sm text-muted-foreground">Candidates remain experimental. A held-out report and a human promotion decision are required before validated use. Missing or unreviewed evidence stops training.</p>
    </div>
  </details>;
}
