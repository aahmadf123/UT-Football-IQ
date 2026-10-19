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

import { processingErrorMessage } from "@/lib/api-error";
import Link from "next/link";
import { ArrowRight, Clapperboard, Upload, Activity, ScanEye, CircleAlert, Film, ChevronRight } from "lucide-react";
import { useCallback } from "react";
import { useAppState, type ApiStatus } from "@/lib/app-state";
import { useFetchState } from "@/lib/fetch-state";
import { fetchAlerts, type ApiAlert, type VideoInboxItem } from "@/lib/api";
import { useUploadWidget } from "@/components/shared/upload-widget";
import { CvSpotlight } from "@/components/cv-spotlight";
import { useWorkspaceContext } from "@/lib/workspace-context";
import type { ApiJob, ApiVideo } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { AsyncSection } from "@/components/composite/async-section";
import { EmptyState } from "@/components/composite/empty-state";
import { StatusBadge, toneForJobStatus, toneForSeverity, toneForVideoStatus } from "@/components/composite/status-badge";

export function DashboardView() {
  const { openFilePicker, widget } = useUploadWidget();
  const workspace = useWorkspaceContext();
  return (
    <>
      {widget}
      <div className="flex flex-col gap-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium">Your next advantage starts on film.</p>
            <p className="mt-1 max-w-3xl text-xs text-muted-foreground">{workspace.description} Review video with tracking, then turn coach corrections into reviewed labels.</p>
          </div>
          <div className="flex gap-2"><Button asChild variant="outline"><Link href="/film-room/">Open Film Room <ArrowRight className="size-4" /></Link></Button><Button onClick={openFilePicker}><Upload className="size-4" /> Upload film</Button></div>
        </div>
        <WorkspaceOverview />
        <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.85fr)_minmax(280px,1fr)]">
          <CvSpotlight />
          <div className="flex flex-col gap-4"><WorkflowCard /><AlertsSummaryCard /></div>
        </div>
        <PracticeInboxSection />
        <RecentFilmCard onUploadClick={openFilePicker} />
      </div>
    </>
  );
}

function WorkspaceOverview() {
  const { data, inboxItems, apiStatus, mockMode } = useAppState();
  const available = apiStatus === "live" && !mockMode;
  const summaries = [
    { label: "Recordings loaded", value: data.videos.length, detail: "Current library view", icon: Film, href: "/film-room/?tab=browse" },
    { label: "Plays detected", value: inboxItems.reduce((count, item) => count + item.clip_count, 0), detail: "Available in the film inbox", icon: ScanEye, href: "/film-room/?tab=review" },
    { label: "Active processing", value: data.jobs.filter((job) => job.status === "running" || job.status === "queued").length, detail: "Queued or running jobs", icon: Activity, href: "/film-room/?tab=upload" },
    { label: "Needs attention", value: inboxItems.filter((item) => item.failed_jobs > 0 || item.video_status === "failed").length, detail: "Recordings with processing errors", icon: CircleAlert, href: "/film-room/?tab=upload" },
  ];
  return <section aria-label="Workspace overview" className="grid grid-cols-2 gap-3 lg:grid-cols-4">{summaries.map(({ label, value, detail, icon: Icon, href }) => <Link key={label} href={href} className="workspace-surface group rounded-lg border border-border-soft p-4 transition-colors hover:border-primary/35"><div className="flex items-center justify-between gap-2"><span className="text-[11px] font-medium text-muted-foreground">{label}</span><Icon className="size-4 text-primary/75" aria-hidden /></div><p data-numeric className="mt-3 text-[1.75rem] font-semibold leading-none tracking-tight">{available ? value : "—"}</p><p className="mt-2 text-[10px] text-muted-foreground">{available ? detail : mockMode ? "Connect real film to see totals" : "Waiting for library connection"}</p></Link>)}</section>;
}

function WorkflowCard() {
  return <Card className="gap-4 py-5"><CardHeader className="px-5"><p className="section-eyebrow">Film to feedback</p><h2 className="text-sm font-semibold">One connected workflow</h2></CardHeader><CardContent className="px-5"><div className="flex flex-col gap-1">{[
    { step: "01", title: "Organize your film", detail: "Sessions, recordings and camera angles", href: "/film-room/?tab=browse" },
    { step: "02", title: "Run the vision pipeline", detail: "Choose a profile and connected processor", href: "/film-room/?tab=upload" },
    { step: "03", title: "Review and improve", detail: "Watch tracking and verify play labels", href: "/film-room/?tab=review" },
  ].map((item) => <Link key={item.step} href={item.href} className="group flex items-center gap-3 rounded-md py-3 hover:bg-secondary/40"><span className="font-mono text-[10px] text-primary/65">{item.step}</span><span className="min-w-0 flex-1"><span className="block text-xs font-medium">{item.title}</span><span className="mt-1 block text-[10px] text-muted-foreground">{item.detail}</span></span><ChevronRight className="size-3.5 text-muted-foreground group-hover:text-primary" aria-hidden /></Link>)}</div></CardContent></Card>;
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
            <h2 className="text-base font-semibold">
              Practice Inbox
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
                  Processing detail <ArrowRight className="size-3.5" />
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
  const { inboxItems, data, mockMode, apiStatus, apiError } = useAppState();
  const workspace = useWorkspaceContext();
  const jobs = data.jobs;

  // Outside mock mode, never substitute a fake job-based view. Distinguish
  // a genuinely empty inbox from "still connecting" or "backend offline" so
  // the UI does not appear successful when the backend has no inbox data or
  // is not reachable.
  if (!mockMode && inboxItems.length === 0) {
    const isLoading = apiStatus === "loading" || apiStatus === "idle";
    const isOffline = apiStatus === "offline" || apiStatus === "error";
    return (
      <EmptyState
        icon={Clapperboard}
        title={
          isLoading
            ? "Connecting to the inbox…"
            : isOffline
              ? workspace.local ? "Your local library is not connected." : "Your team library is not connected."
              : "No videos in the inbox yet."
        }
        hint={
          isLoading || isOffline
            ? isOffline ? apiError ?? `Open ${workspace.settingsLabel} to check the connection, then reconnect.` : undefined
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
      <h3 className="mb-1 text-xs font-semibold text-muted-foreground">
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
              <Badge variant="secondary" className="ml-2 text-[0.62rem]">
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
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border-soft py-3 text-sm last:border-b-0">
      <span className="min-w-0 flex-1 basis-48 truncate font-medium">
        <Link
          href={
            item.clip_count === 0 || item.video_status === "failed"
              ? "/film-room/?tab=upload"
              : `/film-room/?tab=review&videoId=${item.video_id}`
          }
          className="hover:text-primary hover:underline"
        >
          {item.filename}
        </Link>
      </span>
      <span data-numeric className="font-mono text-xs text-muted-foreground">
        {item.succeeded_jobs}/{item.total_jobs} jobs
        {item.clip_count > 0 && ` · ${item.clip_count} clips`}
      </span>
      <StatusBadge tone={toneForVideoStatus(item.video_status)} dot className="capitalize">
        {item.video_status}
      </StatusBadge>
      <Button asChild variant="outline" size="sm">
        <Link href={item.clip_count > 0 ? `/film-room/?tab=review&videoId=${encodeURIComponent(item.video_id)}` : "/film-room/?tab=upload"}>
          {item.clip_count > 0 ? "Review plays" : item.video_status === "uploaded" ? "Process film" : "View progress"}<ArrowRight className="size-3.5" />
        </Link>
      </Button>
      {item.latest_error_message ? (
        <span className="basis-full text-sm text-status-danger">
          {processingErrorMessage(item.latest_error_stage, item.latest_error_message)}
        </span>
      ) : null}
    </div>
  );
}

// ── Recent Film ──────────────────────────────────────────────────────────────

function RecentFilmCard({ onUploadClick }: { onUploadClick: () => void }) {
  const { data, apiStatus } = useAppState();
  const workspace = useWorkspaceContext();
  const videos = [...data.videos]
    .sort((a, b) => (b.recorded_at ?? b.created_at ?? "").localeCompare(a.recorded_at ?? a.created_at ?? ""))
    .slice(0, 6);

  return (
    <section data-testid="dashboard-recent-film">
      <Card className="h-full">
        <CardHeader className="flex flex-row items-center justify-between gap-2">
          <h2 className="text-base font-semibold">
            Recent Film
          </h2>
          <Button size="sm" onClick={onUploadClick}>
            <Upload className="size-4" /> Upload Film
          </Button>
        </CardHeader>
        <CardContent className="flex h-full flex-col">
          {videos.length === 0 ? (
            <p className="text-xs text-muted-foreground">{filmEmptyMessage(apiStatus, workspace.local)}</p>
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
    <div className="flex items-center justify-between gap-3 border-b border-border-soft py-3 last:border-b-0">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border-soft bg-secondary/40"><Film className="size-4 text-muted-foreground" aria-hidden /></span>
      <div className="min-w-0">
        <Link href={`/clip-review/?videoId=${encodeURIComponent(video.id)}`} className="block truncate text-sm font-medium hover:text-primary">{video.filename}</Link>
        <span className="text-xs text-muted-foreground">
          {formatStamp(video.recorded_at ?? video.created_at)}{video.camera_label ? ` · ${video.camera_label}` : ""}{video.opponent_team ? ` · vs. ${video.opponent_team}` : ""}
        </span>
      </div>
      <StatusBadge tone={toneForVideoStatus(video.status)} dot className="capitalize">
        {video.status}
      </StatusBadge>
    </div>
  );
}

function filmEmptyMessage(status: ApiStatus, local: boolean): string {
  switch (status) {
    case "loading":
    case "idle":
      return "Loading film…";
    case "offline":
    case "error":
      return local ? "Open This computer to reconnect to your local library." : "Open Workspace settings to reconnect to your team library.";
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
          <h2 className="text-base font-semibold">
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
        <span className="block truncate text-sm font-medium">{alert.alert_type}</span>
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
