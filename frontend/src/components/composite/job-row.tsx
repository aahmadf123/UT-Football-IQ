"use client";

/**
 * Per-job row with stage-level progress and lease/attempt bookkeeping.
 * Lives with the jobs surface (Film Room → Upload & Processing) — the one
 * home for pipeline detail.
 */

import type { ApiJob, JobStageProgress } from "@/lib/types";
import { StatusBadge, toneForJobStatus } from "@/components/composite/status-badge";
import { cn } from "@/lib/utils";

export function JobRow({ job }: { job: ApiJob }) {
  const stages = summarizeProgress(job.progress);
  const lease = leaseSummary(job);
  return (
    <div
      className="border-b border-border-soft py-2 text-[0.8rem] last:border-b-0"
      data-testid={`job-row-${job.id}`}
    >
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-medium text-foreground">{job.job_type}</span>
        <StatusBadge tone={toneForJobStatus(job.status)} dot className="capitalize">
          {job.status}
        </StatusBadge>
      </div>
      {lease ? (
        <div className="mt-1 text-xs text-muted-foreground" data-testid={`job-lease-${job.id}`}>
          {lease}
        </div>
      ) : null}
      {stages.length > 0 ? (
        <div
          className="mt-1 flex flex-wrap gap-x-2.5 gap-y-0.5 font-mono text-xs text-muted-foreground"
          data-testid={`job-progress-${job.id}`}
        >
          {stages.map(({ stage, status }) => (
            <span key={stage} className="whitespace-nowrap">
              {stage}{" "}
              <span
                className={cn("font-bold", STAGE_CLASS[status])}
                aria-label={`${stage}: ${status}`}
              >
                {STAGE_GLYPH[status]}
              </span>
            </span>
          ))}
        </div>
      ) : null}
      {job.status === "failed" && job.error_message ? (
        <div className="mt-1 text-xs text-status-danger">
          {job.error_stage ? `${job.error_stage}: ` : ""}
          {job.error_message}
        </div>
      ) : null}
    </div>
  );
}

/**
 * One-line lease/attempt summary for a job row (queue bookkeeping from the
 * backend JobResponse). Running jobs surface a retry attempt ("attempt 2")
 * and the worker holding the lease; failed jobs say how many attempts were
 * burned. Returns null when there is nothing noteworthy to show.
 */
export function leaseSummary(
  job: Pick<ApiJob, "status" | "attempt_count" | "leased_by">,
): string | null {
  const attempts = job.attempt_count ?? 0;
  if (job.status === "running") {
    const parts: string[] = [];
    if (attempts > 1) parts.push(`attempt ${attempts}`);
    if (job.leased_by) parts.push(`worker ${job.leased_by}`);
    return parts.length > 0 ? parts.join(" · ") : null;
  }
  if (job.status === "failed" && attempts >= 1) {
    return `failed after ${attempts} attempt${attempts === 1 ? "" : "s"}`;
  }
  return null;
}

type StageStatus = "done" | "running" | "failed" | "skipped" | "pending";

const STAGE_GLYPH: Record<StageStatus, string> = {
  done: "✓",
  running: "●",
  failed: "✕",
  skipped: "–",
  pending: "○",
};

const STAGE_CLASS: Record<StageStatus, string> = {
  done: "text-status-ok",
  running: "text-status-info",
  failed: "text-status-danger",
  skipped: "text-muted-foreground",
  pending: "text-muted-foreground",
};

/**
 * Collapse a job's per-stage progress map into an ordered stage summary.
 * Clip-suffixed keys (`"track:1a2b3c4d"`) fold into their base stage; a stage
 * is running while any of its entries is `started`, failed if any failed,
 * done when everything succeeded (or was skipped).
 */
export function summarizeProgress(
  progress: Record<string, JobStageProgress> | null | undefined,
): Array<{ stage: string; status: StageStatus }> {
  if (!progress) return [];
  const order: string[] = [];
  const byStage = new Map<string, string[]>();
  for (const [key, value] of Object.entries(progress)) {
    const stage = key.split(":")[0];
    if (!byStage.has(stage)) {
      byStage.set(stage, []);
      order.push(stage);
    }
    byStage.get(stage)!.push(String(value?.status ?? ""));
  }
  return order.map((stage) => {
    const statuses = byStage.get(stage)!;
    let status: StageStatus;
    if (statuses.includes("failed")) status = "failed";
    else if (statuses.includes("started")) status = "running";
    else if (statuses.every((s) => s === "skipped")) status = "skipped";
    else if (statuses.every((s) => s === "succeeded" || s === "skipped")) status = "done";
    else status = "pending";
    return { stage, status };
  });
}
