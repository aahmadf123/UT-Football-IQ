"use client";

import { useState } from "react";
import { useAppState } from "@/lib/app-state";
import { getExpectedFeatureDownload, type ExpectedFeatureDownload } from "@/lib/api";
import { friendlyMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";

export function ExpectedFeatureExports() {
  const { authToken, data } = useAppState();
  const [downloads, setDownloads] = useState<Record<string, ExpectedFeatureDownload>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const jobs = data.jobs.filter((job) => job.job_type === "pipeline" && job.status === "succeeded"
    && job.output_artifacts?.expected_feature_export && typeof job.output_artifacts.expected_feature_export === "object");
  if (!jobs.length) return <p className="text-xs text-muted-foreground">No qualified prediction-time feature exports are available yet. Record the required observations and process the original film to prepare a staff target template.</p>;
  return <section aria-label="Download prediction-time feature snapshots" className="space-y-3 rounded border border-border-soft p-3"><h3 className="text-sm font-semibold">Prepare reviewed outcome targets</h3><p className="text-xs text-muted-foreground">Download the actual processor feature snapshots and keep their checksum with the original film. The target template starts blank; staff must enter actual outcomes and review attribution.</p>{jobs.map((job) => {
    const download = downloads[job.id];
    const recording = data.videos?.find((video) => video.id === job.video_id);
    const count = (job.output_artifacts?.expected_feature_export as { row_count?: number }).row_count;
    return <div key={job.id} className="space-y-2 border-t border-border-soft pt-3 text-xs"><p className="font-medium">{recording?.filename ?? "Original recording"}  |  {count ?? "Unknown"} snapshots</p><p>Source processing job <span className="break-all font-mono">{job.id}</span></p><Button size="sm" variant="outline" disabled={busy !== null} onClick={async () => { setBusy(job.id); setError(null); try { const result = await getExpectedFeatureDownload(job.id, authToken); setDownloads((previous) => ({ ...previous, [job.id]: result })); } catch (cause) { setError(friendlyMessage(cause)); } finally { setBusy(null); } }}>{busy === job.id ? "Checking export..." : "Get feature download"}</Button>{download ? <><a className="ml-3 text-primary underline" href={download.download_url} download={download.filename} target="_blank" rel="noopener noreferrer">Download feature snapshots</a><p>{download.row_count} snapshots  |  {download.size_bytes.toLocaleString()} bytes  |  SHA-256 <span className="break-all font-mono">{download.sha256}</span></p><details><summary className="cursor-pointer">Create the blank staff target template</summary><p className="mt-2">Save the download on the processor. From its gpu-worker folder, run:</p><pre className="mt-2 overflow-x-auto rounded border border-border-soft p-2">{`python -m training.expected_outcomes targets-template --snapshots "${download.filename}" --snapshots-sha256 ${download.sha256} --output staff-targets.csv`}</pre><p className="mt-2">Staff fills the actual target, human source, reviewed state, reviewer, review time and train/calibration/holdout split. Keep whole sessions separate. Then freeze those reviewed entries with the original film:</p><pre className="mt-2 overflow-x-auto rounded border border-border-soft p-2">{`python -m training.expected_outcomes prepare --snapshots "${download.filename}" --snapshots-sha256 ${download.sha256} --annotations staff-targets.csv --annotations-sha256 <reviewed-csv-sha256> --media <original-film-path> --output <new-frozen-folder>`}</pre><p className="mt-2">Repeat snapshot/media arguments for additional original recordings. Use the new manifest path and returned checksum below. Existing files and unreviewed or missing values are refused.</p></details></> : null}</div>;
  })}{error ? <p role="alert" className="text-xs text-status-danger">{error}</p> : null}</section>;
}
