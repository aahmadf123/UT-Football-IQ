"use client";

/**
 * Dashboard — real backend surfaces only:
 *
 *   - Tracking Spotlight: the vision model's overlays playing on the latest
 *     processed clip (the product's thesis, first thing on screen)
 *   - Practice Inbox: per-video processing status from /api/v1/inbox/status,
 *     with a pipeline summary line (jobs' detail home is Film Room → Upload &
 *     Processing) and deep links straight into review
 *   - Recent Film: videos from the shared app-state fetch (+ upload trigger)
 *   - Coaching Alerts: compact summary; the one alerts home is /alerts
 *
 * Every card renders honest loading/offline/empty states instead of sample
 * numbers (repo rule #96).
 */

import Link from "next/link";
import { ArrowRight, Clapperboard, Upload } from "lucide-react";
import { useCallback } from "react";
import { useAppState, type ApiStatus } from "@/lib/app-state";
import { useFetchState } from "@/lib/fetch-state";
import { fetchAlerts, type ApiAlert, type VideoInboxItem } from "@/lib/api";
import { useUploadWidget } from "@/components/shared/upload-widget";
import { CvSpotlight } from "@/components/cv-spotlight";
import type { ApiJob, ApiVideo } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { AsyncSection } from "@/components/composite/async-section";
import { EmptyState } from "@/components/composite/empty-state";
import { StatusBadge, toneForJobStatus, toneForSeverity, toneForVideoStatus } from "@/components/composite/status-badge";

export function DashboardView() {
  const { openFilePicker, widget } = useUploadWidget();
  return (
    <>
      {widget}
      <div className="flex flex-col gap-4">
        <CvSpotlight />
        <PracticeInboxSection />
        <div className="grid gap-4 md:grid-cols-2">
          <RecentFilmCard onUploadClick={openFilePicker} />
          <AlertsSummaryCard />
        </div>
      </div>
    </>
  );
}

// ── Practice Inbox ───────────────────────────────────────────────────────────

function PracticeInboxSection() {
  const { inboxItems, data, mockMode } = useAppState();
  const jobs = data.jobs;

  const running = inboxItems.reduce((n, i) => n + i.running_jobs, 0);
  const failed = inboxItems.reduce((n, i) => n + i.failed_jobs, 0);

  return (
    <section>
      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="font-display text-base font-semibold uppercase tracking-wide">
              Practice Inbox — Processing Status
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {mockMode
                ? `${jobs.length} processing job${jobs.length !== 1 ? "s" : ""} (sample)`
                : `${inboxItems.length} video${inboxItems.length !== 1 ? "s" : ""} in inbox`}
            </p>
          </div>
          {!mockMode && inboxItems.length > 0 ? (
            <div
              className="flex items-center gap-2 text-xs text-muted-foreground"
              data-testid="dashboard-jobs"
            >
              {running > 0 ? <StatusBadge tone="info" dot>{running} running</StatusBadge> : null}
              {failed > 0 ? <StatusBadge tone="danger" dot>{failed} failed</StatusBadge> : null}
              <Button asChild variant="link" size="sm" className="h-auto p-0 text-primary">
                <Link href="/film-room/?tab=upload">
                  Pipeline detail <ArrowRight className="size-3.5" />
                </Link>
              </Button>
            </div>
          ) : null}
        </CardHeader>
        <CardContent>
          <InboxBody />
        </CardContent>
      </Card>
    </section>
  );
}

function InboxBody() {
  const { inboxItems, data, mockMode, apiStatus } = useAppState();
  const jobs = data.jobs;

  // Outside mock mode, never substitute a fake job-based view. Distinguish
  // a genuinely empty inbox from "still connecting" or "backend offline" so
  // the UI does not appear successful when the backend has no inbox data or
  // is not reachable.
  if (!mockMode && inboxItems.length === 0) {
    const isLoading = apiStatus === "loading" || apiStatus === "idle";
    const isOffline = apiStatus === "offline";
    return (
      <EmptyState
        icon={Clapperboard}
        title={
          isLoading
            ? "Connecting to the inbox…"
            : isOffline
              ? "Inbox unavailable — not connected to the team server."
              : "No videos in the inbox yet."
        }
        hint={
          isLoading || isOffline
            ? undefined
            : "Upload practice or game film to begin processing."
        }
      />
    );
  }

  // Mock mode shows the legacy job-based view from sample data.
  if (mockMode || inboxItems.length === 0) {
    const sameSession = jobs.filter((j) => j.is_same_session || j.pipeline_mode === "same_session");
    const nightly = jobs.filter((j) => !j.is_same_session && j.pipeline_mode !== "same_session");
    return (
      <div className="flex flex-col gap-4">
        {jobs.length === 0 ? (
          <EmptyState
            icon={Clapperboard}
            title="No processing jobs yet"
            hint="Upload video to begin."
          />
        ) : null}
        {sameSession.length > 0 ? (
          <MockJobGroup label="Same-Session (period-break)" jobs={sameSession} />
        ) : null}
        {nightly.length > 0 ? <MockJobGroup label="Nightly (full quality)" jobs={nightly} /> : null}
      </div>
    );
  }

  // Real backend-backed inbox view
  return (
    <div className="flex flex-col">
      {inboxItems.map((item) => (
        <InboxVideoRow key={item.video_id} item={item} />
      ))}
    </div>
  );
}

function MockJobGroup({ label, jobs }: { label: string; jobs: ApiJob[] }) {
  return (
    <div>
      <h3 className="mb-1 font-display text-xs font-semibold uppercase tracking-widest text-muted-foreground">
        {label}
      </h3>
      {jobs.map((j) => {
        const sameSession = j.pipeline_mode === "same_session" || j.is_same_session;
        return (
          <div
            key={j.id}
            className="flex items-center gap-2 border-b border-border-soft py-1.5 text-[0.8rem] last:border-b-0"
          >
            <span className="min-w-0 flex-1 truncate font-medium">
              {j.job_type}
              <Badge variant="secondary" className="ml-2 text-[0.62rem] uppercase">
                {sameSession ? "Same-Session" : "Nightly"}
              </Badge>
            </span>
            <StatusBadge tone={toneForJobStatus(j.status)} dot className="capitalize">
              {j.status}
            </StatusBadge>
          </div>
        );
      })}
    </div>
  );
}

function InboxVideoRow({ item }: { item: VideoInboxItem }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border-soft py-2 text-[0.8rem] last:border-b-0">
      <span className="min-w-0 flex-1 basis-48 truncate font-medium">
        <Link
          href={
            item.video_status === "failed"
              ? "/film-room/?tab=upload"
              : `/film-room/?tab=review&videoId=${item.video_id}`
          }
          className="hover:text-primary hover:underline"
        >
          {item.filename}
        </Link>
        {item.same_session_job_count > 0 ? (
          <Badge variant="secondary" className="ml-2 text-[0.62rem] uppercase">
            {item.same_session_job_count} same-session
          </Badge>
        ) : null}
      </span>
      <span data-numeric className="font-mono text-xs text-muted-foreground">
        {item.succeeded_jobs}/{item.total_jobs} jobs
        {item.clip_count > 0 && ` · ${item.clip_count} clips`}
        {item.calibration_safe_pct !== null && ` · ${item.calibration_safe_pct}% safe`}
      </span>
      <StatusBadge tone={toneForVideoStatus(item.video_status)} dot className="capitalize">
        {item.video_status}
      </StatusBadge>
      {item.latest_error_message ? (
        <span
          title={item.latest_error_message}
          className="cursor-help text-xs text-status-danger"
        >
          {item.latest_error_stage ?? "Error"}
        </span>
      ) : null}
    </div>
  );
}

// ── Recent Film ──────────────────────────────────────────────────────────────

function RecentFilmCard({ onUploadClick }: { onUploadClick: () => void }) {
  const { data, apiStatus } = useAppState();
  const videos = [...data.videos]
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""))
    .slice(0, 6);

  return (
    <section data-testid="dashboard-recent-film">
      <Card className="h-full">
        <CardHeader className="flex flex-row items-center justify-between gap-2">
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Recent Film
          </h2>
          <Button size="sm" onClick={onUploadClick}>
            <Upload className="size-4" /> Upload Film
          </Button>
        </CardHeader>
        <CardContent className="flex h-full flex-col">
          {videos.length === 0 ? (
            <p className="text-xs text-muted-foreground">{filmEmptyMessage(apiStatus)}</p>
          ) : (
            <div className="flex flex-col">
              {videos.map((v) => (
                <VideoLine key={v.id} video={v} />
              ))}
            </div>
          )}
          <Button asChild variant="link" size="sm" className="mt-auto h-auto self-start p-0 pt-3 text-primary">
            <Link href="/film-room/?tab=browse">
              Browse all film <ArrowRight className="size-3.5" />
            </Link>
          </Button>
        </CardContent>
      </Card>
    </section>
  );
}

function VideoLine({ video }: { video: ApiVideo }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border-soft py-2 last:border-b-0">
      <div className="min-w-0">
        <span className="block truncate text-[0.82rem] font-medium">{video.filename}</span>
        <span className="text-xs text-muted-foreground">
          {formatStamp(video.recorded_at ?? video.created_at)}
        </span>
      </div>
      <StatusBadge tone={toneForVideoStatus(video.status)} dot className="capitalize">
        {video.status}
      </StatusBadge>
    </div>
  );
}

function filmEmptyMessage(status: ApiStatus): string {
  switch (status) {
    case "loading":
    case "idle":
      return "Loading film…";
    case "offline":
      return "Film list unavailable — not connected to the team server.";
    default:
      return "No film yet. Upload practice or game video to get started.";
  }
}

// ── Coaching Alerts summary ──────────────────────────────────────────────────

function AlertsSummaryCard() {
  const { authToken } = useAppState();
  const fetcher = useCallback(() => fetchAlerts({ limit: 5 }, authToken), [authToken]);
  const { state, reload } = useFetchState(fetcher);
  const restricted =
    state.kind === "error" &&
    /position group access is restricted|\(403\)/i.test(state.message);

  return (
    <section data-testid="dashboard-alerts">
      <Card className="h-full">
        <CardHeader>
          <h2 className="font-display text-base font-semibold uppercase tracking-wide">
            Coaching Alerts
          </h2>
        </CardHeader>
        <CardContent className="flex h-full flex-col">
          {restricted ? (
            <p className="text-xs text-muted-foreground">
              Alerts are restricted for your assigned position group. Sign in with a role that has
              alerts access to view this panel.
            </p>
          ) : (
            <AsyncSection
              state={state}
              onRetry={reload}
              loadingLabel="Loading alerts"
              offlineTitle="Alerts unavailable — not connected to the team server"
              emptyTitle="No alerts generated yet"
              emptyHint="They appear as the pipeline processes film."
            >
              {(alerts) => (
                <div className="flex flex-col">
                  {alerts.map((a) => (
                    <AlertLine key={a.id} alert={a} />
                  ))}
                </div>
              )}
            </AsyncSection>
          )}
          <Button asChild variant="link" size="sm" className="mt-auto h-auto self-start p-0 pt-3 text-primary">
            <Link href="/alerts">
              Open alerts inbox <ArrowRight className="size-3.5" />
            </Link>
          </Button>
        </CardContent>
      </Card>
    </section>
  );
}

function AlertLine({ alert }: { alert: ApiAlert }) {
  return (
    <div className="flex items-center justify-between gap-2 border-b border-border-soft py-2 last:border-b-0">
      <div className="min-w-0">
        <span className="block truncate text-[0.82rem] font-medium">{alert.alert_type}</span>
        <span className="text-xs text-muted-foreground">
          {alert.position_group} · {alert.metric_name} · {Math.round(alert.confidence * 100)}%
        </span>
      </div>
      <StatusBadge tone={toneForSeverity(alert.severity)} className="capitalize">
        {alert.severity}
      </StatusBadge>
    </div>
  );
}

function formatStamp(value: string | null | undefined): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return value;
  }
}
