"use client";

import Link from "next/link";
import { Upload } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { PreliminaryBadge } from "@/components/clip-state-badge";
import { UploadStatusList } from "@/components/shared/upload-status-list";
import {
  fetchAllJobs,
  fetchJobs,
  fetchVideo,
  fetchSystemStatus,
  controlJob,
  requestVideoProcessing,
  retryJob,
  WorkloadGatedError,
  type VideoInboxItem,
} from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { useFetchState } from "@/lib/fetch-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { JobRow } from "@/components/composite/job-row";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import type { ApiJob, ProcessingProfile } from "@/lib/types";
import { useWorkspaceContext } from "@/lib/workspace-context";
import { friendlyMessage, processingErrorMessage } from "@/lib/api-error";

// Per-video local request state, layered on top of the backend inbox status so
// the coach gets immediate "Queued" feedback before the backend reflects it.
type RequestState =
  | { kind: "queued"; baselineJobs: number }
  | { kind: "busy"; message: string }
  | { kind: "error"; message: string };

// Coach-facing label + tone for a video's processing state. Derived from the
// backend inbox item (video status + job counts) first — the video row stays
// "uploaded" in the DB until the pipeline reports back, so queued/running job
// counts are the real signal that processing was requested. The local request
// state only layers instant feedback on top; it dies when the tab unmounts and
// must never be the sole source of truth.
function statusBadge(
  item: VideoInboxItem,
  request: RequestState | undefined,
  paused: boolean,
): { label: string; tone: StatusTone } {
  const queued = (item.queued_jobs ?? 0) > 0 || request?.kind === "queued";
  if (item.running_jobs > 0) return { label: "Processing", tone: "info" };
  if (queued) return { label: "Queued", tone: "warn" };
  if (paused) return { label: "Paused", tone: "neutral" };
  switch (item.video_status) {
    case "ready":
      return { label: "Processed", tone: "ok" };
    case "processing":
      return { label: "Processing", tone: "info" };
    case "failed":
      // A failed video with a retry already queued reads "Queued", not "Failed".
      return queued ? { label: "Queued", tone: "warn" } : { label: "Failed", tone: "danger" };
    case "uploaded":
    default:
      if (item.running_jobs > 0) return { label: "Processing", tone: "info" };
      if (queued) return { label: "Queued", tone: "warn" };
      return { label: "Uploaded", tone: "neutral" };
  }
}

export function UploadProcessFilm({ onUploadClick }: { onUploadClick: () => void }) {
  const {
    uploads,
    inboxItems,
    refreshInbox,
    apiStatus,
    mockMode,
    authToken,
    data,
    currentRole,
  } = useAppState();
  const workspace = useWorkspaceContext();
  const canProcess = ["admin", "analyst", "coach", "sportsperformance"].includes(currentRole);

  const [requests, setRequests] = useState<Record<string, RequestState>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [profile, setProfile] = useState<ProcessingProfile>("quick");
  const runtimeFetcher = useCallback(() => fetchSystemStatus(authToken), [authToken]);
  const { state: runtime, reload: reloadRuntime } = useFetchState(runtimeFetcher);
  const status = runtime.kind === "ready" ? runtime.data : null;
  const selectedProfile = status?.profiles?.find((item) => item.id === profile);
  const profileUnavailable = selectedProfile
    ? !selectedProfile.available
    : profile !== "review" && status?.capabilities?.processing === false;
  useEffect(() => {
    if (status?.profiles?.find((item) => item.id === profile)?.available === false) {
      const available = status.profiles.find((item) => item.available);
      if (available) setProfile(available.id);
    }
  }, [status, profile]);
  useEffect(() => {
    setRequests((current) => Object.fromEntries(Object.entries(current).filter(([id, request]) => request.kind !== "queued" || !inboxItems.some((item) => item.video_id === id && (item.total_jobs > request.baselineJobs || item.running_jobs > 0 || (item.queued_jobs ?? 0) > 0)))));
  }, [inboxItems]);

  const runRequest = useCallback(
    async (videoId: string, action: () => Promise<unknown>) => {
      setPending((cur) => ({ ...cur, [videoId]: true }));
      setRequests((cur) => {
        const next = { ...cur };
        delete next[videoId];
        return next;
      });
      try {
        await action();
        setRequests((cur) => ({ ...cur, [videoId]: { kind: "queued", baselineJobs: inboxItems.find((item) => item.video_id === videoId)?.total_jobs ?? 0 } }));
        refreshInbox();
      } catch (err) {
        if (err instanceof WorkloadGatedError) {
          setRequests((cur) => ({ ...cur, [videoId]: { kind: "busy", message: err.message } }));
        } else {
          setRequests((cur) => ({
            ...cur,
            [videoId]: {
              kind: "error",
              message: friendlyMessage(err),
            },
          }));
        }
      } finally {
        setPending((cur) => ({ ...cur, [videoId]: false }));
      }
    },
    [refreshInbox, inboxItems],
  );

  const handleProcess = useCallback(
    (videoId: string) =>
      runRequest(videoId, () =>
        requestVideoProcessing(videoId, { profile }, authToken),
      ),
    [authToken, runRequest, profile],
  );

  // Failed videos retry the failed job itself (POST /jobs/{id}/retry) so the
  // job lineage/attempt counters are preserved; if the failed job row is no
  // longer visible, fall back to enqueueing a fresh pipeline job.
  const handleRetry = useCallback(
    (videoId: string) =>
      runRequest(videoId, async () => {
        const failed = await fetchJobs(
          { video_id: videoId, status: "failed", limit: 1 },
          authToken,
        );
        if (failed.length > 0) {
          await retryJob(failed[0].id, authToken);
        } else {
          await requestVideoProcessing(videoId, { profile }, authToken);
        }
      }),
    [authToken, runRequest, profile],
  );

  // Keep duplicate outcomes visible so coaches can open the preserved film
  // after a completed upload, rather than losing the result with the spinner.
  const uploadActivity = uploads.filter((u) => u.phase !== "done" || u.duplicate);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Upload &amp; Process Film
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Save practice or game film in your {workspace.local ? "local" : "team"} library. Choose a processing profile and start any recording below. Progress and results are saved with the film.
            </p>
          </div>
          <Button onClick={onUploadClick} disabled={!canProcess}>
            <Upload className="size-4" /> Upload Film
          </Button>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <Label htmlFor="processing-profile">Processing profile</Label>
          <NativeSelect id="processing-profile" value={profile} onChange={(event) => setProfile(event.target.value as ProcessingProfile)}>
            <option value="review" disabled={status?.profiles?.find((item) => item.id === "review")?.available === false}>Film review — segment and play video</option>
            <option value="quick" disabled={status?.profiles?.find((item) => item.id === "quick")?.available === false}>Fast feedback</option>
            <option value="detailed" disabled={status?.profiles?.find((item) => item.id === "detailed")?.available === false}>Detailed analysis</option>
          </NativeSelect>
          <p className="text-xs text-muted-foreground">{selectedProfile?.description ?? "Quick tracking uses lighter models. Detailed analysis takes longer. Processing speed depends on the connected processor and recording."}</p>
          {status?.processing_limits?.message ? <p className="rounded-lg border border-border-soft bg-secondary/30 p-3 text-xs leading-relaxed text-muted-foreground">{status.processing_limits.message}</p> : null}
          {profileUnavailable ? <p role="status" className="text-sm text-status-warn">{status?.worker?.reason ?? "No connected processor is ready for this profile."}<Button size="sm" variant="link" onClick={reloadRuntime}>Check again</Button></p> : status && selectedProfile?.available ? <p role="status" className="text-xs text-status-ok">{workspace.local ? "Local processor" : "Team processor"} available for {selectedProfile.label.toLowerCase()}.</p> : null}
          {!canProcess ? <p className="text-xs text-muted-foreground">Your account has read-only film access. Authorized staff can upload and process recordings.</p> : null}
          {runtime.kind === "error" && <p role="status" className="text-xs text-status-warn">Could not check the connected processor. <Button size="sm" variant="link" onClick={reloadRuntime}>Check again</Button></p>}
        </CardContent>
      </Card>

      {uploadActivity.length > 0 && (
        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Upload activity
            </h2>
          </CardHeader>
          <CardContent>
            <UploadStatusList uploads={uploadActivity} />
          </CardContent>
        </Card>
      )}

      <PipelineJobsCard />

      <Card>
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Your film
          </h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Every upload, newest first. Processed film opens straight into its plays.
          </p>
        </CardHeader>
        <CardContent>
          <ProcessingList
            items={inboxItems}
            apiStatus={apiStatus}
            mockMode={mockMode}
            requests={requests}
            pending={pending}
            processingUnavailable={!canProcess || profileUnavailable}
            pausedVideoIds={new Set(data.jobs.filter((job) => job.status === "paused" && job.video_id).map((job) => job.video_id!))}
            onProcess={handleProcess}
            onRetry={handleRetry}
          />
        </CardContent>
      </Card>
    </div>
  );
}

/** Nightly bookkeeping the backend schedules itself; never a coach's concern. */
const MAINTENANCE_JOB_TYPES = new Set(["workload_rollup", "train", "labels", "metrics", "embeddings"]);
const ACTIVE_STATUSES = new Set(["queued", "running", "paused"]);

/** Film jobs only: what is processing now, what failed, then history. */
export function visibleJobs(jobs: ApiJob[]): { current: ApiJob[]; history: ApiJob[] } {
  const film = jobs.filter((job) => job.video_id && !MAINTENANCE_JOB_TYPES.has(job.job_type));
  return {
    current: film.filter((job) => ACTIVE_STATUSES.has(job.status) || job.status === "failed"),
    history: film.filter((job) => !ACTIVE_STATUSES.has(job.status) && job.status !== "failed"),
  };
}

/**
 * The one home for processing-job detail (per-stage progress, lease/attempt
 * bookkeeping), consolidated from the dashboard, model pipeline panel and
 * settings lists. Shows film jobs only; scheduler maintenance stays hidden.
 */
function PipelineJobsCard() {
  const { authToken, data, refreshData, apiError, mockMode } = useAppState();
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<ApiJob[]>([]);
  const [videoFilenames, setVideoFilenames] = useState<Record<string, string | null>>({});
  const loadJobs = useCallback(async () => {
    try {
      setJobs(await fetchAllJobs({ has_video: true }, authToken));
    } catch (cause) {
      setError(friendlyMessage(cause));
    }
  }, [authToken]);
  useEffect(() => {
    void loadJobs();
    const interval = window.setInterval(() => void loadJobs(), 5000);
    return () => window.clearInterval(interval);
  }, [loadJobs]);
  useEffect(() => {
    let cancelled = false;
    const knownVideoIds = new Set(data.videos.map((video) => video.id));
    const missingIds = [...new Set(jobs.flatMap((job) => job.video_id ? [job.video_id] : []))]
      .filter((id) => !knownVideoIds.has(id) && videoFilenames[id] === undefined);
    if (missingIds.length === 0) return;
    void Promise.all(missingIds.map(async (id) => {
      try {
        return [id, (await fetchVideo(id, authToken)).filename] as const;
      } catch {
        return [id, null] as const;
      }
    })).then((filenames) => {
      if (!cancelled) {
        setVideoFilenames((current) => ({
          ...current,
          ...Object.fromEntries(filenames),
        }));
      }
    });
    return () => { cancelled = true; };
  }, [authToken, data.videos, jobs, videoFilenames]);
  const act = async (id: string, action: "pause" | "resume" | "cancel" | "retry") => {
    setPending(id);
    setError(null);
    try {
      if (action === "retry") await retryJob(id, authToken);
      else await controlJob(id, action, authToken);
      await Promise.all([refreshData(), loadJobs()]);
    } catch (cause) { setError(friendlyMessage(cause)); }
    finally { setPending(null); }
  };
  const filenames = new Map(data.videos.map((video) => [video.id, video.filename] as const));
  const label = (job: ApiJob) => filenames.get(job.video_id ?? "") ?? videoFilenames[job.video_id ?? ""] ?? `Film · ${job.video_id?.slice(0, 8) ?? ""}`;
  const { current, history } = visibleJobs(jobs);

  const row = (job: ApiJob) => (
    <div key={job.id}>
      <JobRow job={job} label={label(job)} errorMessage={job.error_message ? processingErrorMessage(job.error_stage, job.error_message) : undefined} />
      {job.control_requested && <p role="status" className="mb-2 text-xs text-status-warn">{job.control_requested === "pause" ? "Pausing processing…" : "Cancelling processing…"}</p>}
      {!mockMode && <div className="mb-3 flex flex-wrap gap-2">
        {["queued", "running"].includes(job.status) && <Button size="sm" variant="outline" disabled={pending === job.id || Boolean(job.control_requested)} onClick={() => void act(job.id, "pause")}>Pause</Button>}
        {job.status === "paused" && <Button size="sm" variant="outline" disabled={pending === job.id} onClick={() => void act(job.id, "resume")}>Resume</Button>}
        {ACTIVE_STATUSES.has(job.status) && <Button size="sm" variant="outline" disabled={pending === job.id || job.control_requested === "cancel"} onClick={() => void act(job.id, "cancel")}>Cancel processing</Button>}
        {job.status === "failed" && <Button size="sm" variant="outline" disabled={pending === job.id} onClick={() => void act(job.id, "retry")}>Retry</Button>}
      </div>}
    </div>
  );

  return (
    <Card data-testid="pipeline-jobs">
      <CardHeader>
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">
          Processing queue
        </h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Film being processed on the team&apos;s processing computer, with stage-by-stage progress.
        </p>
      </CardHeader>
      <CardContent>
        {(error || apiError) && <p role="alert" className="text-xs text-status-danger">{error ?? apiError}</p>}
        {current.length === 0 && <p className="text-sm text-muted-foreground">Nothing processing right now.</p>}
        <div className="flex flex-col">{current.slice(0, 50).map(row)}</div>
        {history.length > 0 && (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs font-medium text-muted-foreground">History ({history.length})</summary>
            <div className="mt-2 flex flex-col">{history.slice(0, 20).map((job) => <JobRow key={job.id} job={job} label={label(job)} />)}</div>
          </details>
        )}
      </CardContent>
    </Card>
  );
}

function ProcessingList({
  items,
  apiStatus,
  mockMode,
  requests,
  pending,
  onProcess,
  onRetry,
  processingUnavailable,
  pausedVideoIds,
}: {
  items: VideoInboxItem[];
  apiStatus: ReturnType<typeof useAppState>["apiStatus"];
  mockMode: boolean;
  requests: Record<string, RequestState>;
  pending: Record<string, boolean>;
  onProcess: (videoId: string) => void;
  onRetry: (videoId: string) => void;
  processingUnavailable: boolean;
  pausedVideoIds: ReadonlySet<string>;
}) {
  if (items.length === 0) {
    const message =
      apiStatus === "loading" || apiStatus === "idle"
        ? "Checking for uploaded film…"
        : apiStatus === "offline"
          ? "Backend offline — uploaded film appears when the team server is connected."
          : mockMode
            ? "Mock mode is on — only server-backed film appears here. Configure the backend to process real film."
            : "No film waiting. Upload practice or game film above, then start processing when you're ready.";
    return (
      <p className="text-xs text-muted-foreground" data-testid="processing-empty">
        {message}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2.5">
      {items.map((item) => (
        <ProcessingRow
          key={item.video_id}
          item={item}
          request={requests[item.video_id]}
          pending={!!pending[item.video_id]}
          onProcess={onProcess}
          onRetry={onRetry}
            processingUnavailable={processingUnavailable}
            paused={pausedVideoIds.has(item.video_id)}
        />
      ))}
    </div>
  );
}

function ProcessingRow({
  item,
  request,
  pending,
  onProcess,
  onRetry,
  processingUnavailable,
  paused,
}: {
  item: VideoInboxItem;
  request: RequestState | undefined;
  pending: boolean;
  onProcess: (videoId: string) => void;
  onRetry: (videoId: string) => void;
  processingUnavailable: boolean;
  paused: boolean;
}) {
  // Server job counts, not just the local click state, gate the buttons: a
  // video with a job already queued or running must never offer "Process Film"
  // again — repeat clicks were silently stacking duplicate pipeline jobs.
  const isQueued = request?.kind === "queued" || (item.queued_jobs ?? 0) > 0;
  const badge = statusBadge(item, request, paused);
  const canProcess = ["uploaded", "ready"].includes(item.video_status) && !isQueued && !paused && item.running_jobs === 0;
  const isFailed = item.video_status === "failed" && !isQueued;

  return (
    <div
      data-testid={`processing-row-${item.video_id}`}
      className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border-soft p-3"
    >
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[0.85rem] font-semibold">{item.filename}</span>
          <span data-testid={`processing-status-${item.video_id}`}>
            <StatusBadge tone={badge.tone} dot>
              {badge.label}
            </StatusBadge>
          </span>
          {(item.preliminary_clip_count ?? 0) > 0 && <PreliminaryBadge />}
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          {badge.label === "Uploaded"
            ? "Not processed yet — start processing when you're ready."
            : badge.label === "Queued"
              ? "Queued — starts as soon as the processing computer picks it up."
              : badge.label === "Processing"
                ? `Processing… ${item.succeeded_jobs}/${item.total_jobs} jobs done`
                : badge.label === "Paused"
                  ? "Paused — resume from the processing job below."
                : badge.label === "Processed"
                  ? `Processed · ${item.clip_count} clip${item.clip_count === 1 ? "" : "s"}`
                  : `Processing failed${item.latest_error_stage ? ` at ${item.latest_error_stage}` : ""}.`}
        </div>
        {(item.preliminary_clip_count ?? 0) > 0 && (
          <div
            className="mt-0.5 text-xs text-muted-foreground"
            data-testid={`processing-preliminary-${item.video_id}`}
          >
            {item.preliminary_clip_count} preliminary clip
            {item.preliminary_clip_count === 1 ? "" : "s"} — detailed analysis can upgrade these results.
          </div>
        )}
        {isFailed && item.latest_error_message && (
          <div className="mt-0.5 text-xs text-status-danger">{processingErrorMessage(item.latest_error_stage, item.latest_error_message)}</div>
        )}
        {request?.kind === "busy" && (
          <div
            className="mt-0.5 text-xs text-status-warn"
            data-testid={`processing-busy-${item.video_id}`}
          >
            {request.message}
          </div>
        )}
        {request?.kind === "error" && (
          <div
            className="mt-0.5 text-xs text-status-danger"
            data-testid={`processing-error-${item.video_id}`}
          >
            {request.message}
          </div>
        )}
      </div>
      {badge.label === "Processed" && item.clip_count > 0 && (
        <Button asChild size="sm" data-testid={`review-plays-${item.video_id}`}>
          <Link href={`/film-room/?tab=review&videoId=${encodeURIComponent(item.video_id)}`}>Review plays</Link>
        </Button>
      )}
      {canProcess && (
        <Button
          size="sm"
          variant={badge.label === "Processed" ? "outline" : "default"}
          data-testid={`process-film-${item.video_id}`}
          onClick={() => onProcess(item.video_id)}
          disabled={pending || processingUnavailable}
        >
          {pending ? "Starting…" : item.video_status === "ready" ? "Reprocess film" : "Process Film"}
        </Button>
      )}
      {isFailed && (
        <Button
          size="sm"
          variant="outline"
          data-testid={`retry-process-${item.video_id}`}
          onClick={() => onRetry(item.video_id)}
          disabled={pending || processingUnavailable}
        >
          {pending ? "Starting…" : "Retry processing"}
        </Button>
      )}
    </div>
  );
}
