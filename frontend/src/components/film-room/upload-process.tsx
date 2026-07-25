"use client";

import { Upload } from "lucide-react";
import { useCallback, useState } from "react";
import { PreliminaryBadge } from "@/components/clip-state-badge";
import { UploadStatusList } from "@/components/shared/upload-status-list";
import {
  fetchJobs,
  requestVideoProcessing,
  retryJob,
  WorkloadGatedError,
  type VideoInboxItem,
} from "@/lib/api";
import { useAppState } from "@/lib/app-state";
import { useFetchState } from "@/lib/fetch-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { AsyncSection } from "@/components/composite/async-section";
import { JobRow } from "@/components/composite/job-row";
import { StatusBadge, type StatusTone } from "@/components/composite/status-badge";

// Per-video local request state, layered on top of the backend inbox status so
// the coach gets immediate "Queued" feedback before the backend reflects it.
type RequestState =
  | { kind: "queued" }
  | { kind: "busy"; message: string }
  | { kind: "error"; message: string };

// Coach-facing label + tone for a backend video status. The five lifecycle
// states (uploaded → queued → processing → processed → failed) are preserved.
function statusBadge(
  videoStatus: string,
  request: RequestState | undefined,
): { label: string; tone: StatusTone } {
  if (request?.kind === "queued" && videoStatus === "uploaded") {
    return { label: "Queued", tone: "warn" };
  }
  switch (videoStatus) {
    case "ready":
      return { label: "Processed", tone: "ok" };
    case "processing":
      return { label: "Processing", tone: "info" };
    case "failed":
      return { label: "Failed", tone: "danger" };
    case "uploaded":
    default:
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
  } = useAppState();

  const [requests, setRequests] = useState<Record<string, RequestState>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});

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
        setRequests((cur) => ({ ...cur, [videoId]: { kind: "queued" } }));
        refreshInbox();
      } catch (err) {
        if (err instanceof WorkloadGatedError) {
          setRequests((cur) => ({ ...cur, [videoId]: { kind: "busy", message: err.message } }));
        } else {
          setRequests((cur) => ({
            ...cur,
            [videoId]: {
              kind: "error",
              message: err instanceof Error ? err.message : String(err),
            },
          }));
        }
      } finally {
        setPending((cur) => ({ ...cur, [videoId]: false }));
      }
    },
    [refreshInbox],
  );

  const handleProcess = useCallback(
    (videoId: string) =>
      runRequest(videoId, () =>
        requestVideoProcessing(videoId, { mode: "nightly" }, authToken),
      ),
    [authToken, runRequest],
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
          await requestVideoProcessing(videoId, { mode: "nightly" }, authToken);
        }
      }),
    [authToken, runRequest],
  );

  const inFlight = uploads.filter((u) => u.phase !== "done");

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Upload &amp; Process Film
            </h2>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              Add practice or game film here. Uploaded film is saved but{" "}
              <strong className="text-foreground">not processed automatically</strong> — click{" "}
              <strong className="text-foreground">Process Film</strong>
              {" when you’re ready to run the pipeline. Full-quality processing runs in the overnight queue."}
            </p>
          </div>
          <Button onClick={onUploadClick}>
            <Upload className="size-4" /> Upload Film
          </Button>
        </CardHeader>
      </Card>

      {inFlight.length > 0 && (
        <Card>
          <CardHeader>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Uploading now
            </h2>
          </CardHeader>
          <CardContent>
            <UploadStatusList uploads={inFlight} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Film &amp; processing status
          </h2>
        </CardHeader>
        <CardContent>
          <ProcessingList
            items={inboxItems}
            apiStatus={apiStatus}
            mockMode={mockMode}
            requests={requests}
            pending={pending}
            onProcess={handleProcess}
            onRetry={handleRetry}
          />
        </CardContent>
      </Card>

      <PipelineJobsCard />
    </div>
  );
}

/**
 * The one home for pipeline job detail (per-stage progress, lease/attempt
 * bookkeeping) — consolidated here from the dashboard jobs card, the old
 * right-rail Model Pipeline panel, and the settings jobs list.
 */
function PipelineJobsCard() {
  const { authToken } = useAppState();
  const fetcher = useCallback(() => fetchJobs({ limit: 20 }, authToken), [authToken]);
  const { state, reload } = useFetchState(fetcher);

  return (
    <Card data-testid="pipeline-jobs">
      <CardHeader>
        <h2 className="font-display text-base font-semibold uppercase tracking-wide">
          Pipeline jobs
        </h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Stage-level progress and retry bookkeeping for every processing job.
        </p>
      </CardHeader>
      <CardContent>
        <AsyncSection
          state={state}
          onRetry={reload}
          loadingLabel="Loading jobs"
          offlineTitle="Jobs unavailable — not connected to the team server"
          emptyTitle="No processing jobs yet"
          emptyHint="Upload film and press Process Film to start the pipeline."
        >
          {(jobs) => (
            <div className="flex flex-col">
              {jobs.map((job) => (
                <JobRow key={job.id} job={job} />
              ))}
            </div>
          )}
        </AsyncSection>
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
}: {
  items: VideoInboxItem[];
  apiStatus: ReturnType<typeof useAppState>["apiStatus"];
  mockMode: boolean;
  requests: Record<string, RequestState>;
  pending: Record<string, boolean>;
  onProcess: (videoId: string) => void;
  onRetry: (videoId: string) => void;
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
}: {
  item: VideoInboxItem;
  request: RequestState | undefined;
  pending: boolean;
  onProcess: (videoId: string) => void;
  onRetry: (videoId: string) => void;
}) {
  const isQueued = request?.kind === "queued";
  const badge = isQueued
    ? { label: "Queued", tone: "warn" as StatusTone }
    : statusBadge(item.video_status, request);
  const canProcess = item.video_status === "uploaded" && !isQueued;
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
          {item.video_status === "uploaded" && !isQueued
            ? "Not processed yet — start processing when you're ready."
            : isQueued
              ? "Queued for full-quality processing."
              : item.video_status === "processing"
                ? `Processing… ${item.succeeded_jobs}/${item.total_jobs} jobs done`
                : item.video_status === "ready"
                  ? `Processed · ${item.clip_count} clip${item.clip_count === 1 ? "" : "s"}`
                  : item.video_status === "failed"
                    ? `Processing failed${item.latest_error_stage ? ` at ${item.latest_error_stage}` : ""}.`
                    : null}
        </div>
        {(item.preliminary_clip_count ?? 0) > 0 && (
          <div
            className="mt-0.5 text-xs text-muted-foreground"
            data-testid={`processing-preliminary-${item.video_id}`}
          >
            {item.preliminary_clip_count} preliminary clip
            {item.preliminary_clip_count === 1 ? "" : "s"} — full-quality nightly upgrade pending.
          </div>
        )}
        {isFailed && item.latest_error_message && (
          <div className="mt-0.5 text-xs text-status-danger">{item.latest_error_message}</div>
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
      {canProcess && (
        <Button
          size="sm"
          data-testid={`process-film-${item.video_id}`}
          onClick={() => onProcess(item.video_id)}
          disabled={pending}
        >
          {pending ? "Starting…" : "Process Film"}
        </Button>
      )}
      {isFailed && (
        <Button
          size="sm"
          variant="outline"
          data-testid={`retry-process-${item.video_id}`}
          onClick={() => onRetry(item.video_id)}
          disabled={pending}
        >
          {pending ? "Starting…" : "Retry processing"}
        </Button>
      )}
    </div>
  );
}
